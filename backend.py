# backend.py
import os, threading, math, csv
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import json, requests

from PySide6.QtCore import QObject, Signal, Slot, QTimer, QDateTime, QLocale

# Optionale Pakete (robust importieren)
try:
    import feedparser
except Exception:
    feedparser = None

try:
    from ics import Calendar
except Exception:
    Calendar = None


# ───────────────────────────── MeteoSwiss Open Data (SwissMetNet) ─────────────────────────────
MCH_BASE = "https://data.geo.admin.ch"
MCH_COLLECTION = "ch.meteoschweiz.ogd-smn"
MCH_STATIONS_CSV = f"{MCH_BASE}/{MCH_COLLECTION}/ogd-smn_meta_stations.csv"

def load_config():
    """Lädt /data/config.json, liefert Defaults wenn nicht vorhanden."""
    here = os.path.dirname(__file__)
    p = os.path.join(here, "data", "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # sinnvolle Defaults
    return {
        "lat": 47.3769,
        "lon": 8.5417,
        "ics_url": "",
        "use_meteoswiss": True,
        "mch_station": "",
        "news_feeds": ["https://www.tagesschau.de/index~rss2.xml"]
    }

def csv_get(url, timeout=10):
    """Liest Semikolon/CP1252-CSV von MeteoSwiss als Dict-Liste."""
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "SmartMirror/1.0"})
    r.raise_for_status()
    text = r.content.decode("cp1252", errors="replace")
    return list(csv.DictReader(text.splitlines(), delimiter=';'))

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def nearest_station(lat, lon, stations):
    best = None
    for r in stations:
        try:
            s_lat = float(r.get("Latitude", r.get("latitude", "nan")))
            s_lon = float(r.get("Longitude", r.get("longitude", "nan")))
            code  = (r.get("Abbr") or r.get("abbr","")).strip()
            name  = (r.get("Name") or r.get("name", code)).strip()
            if not code or math.isnan(s_lat) or math.isnan(s_lon):
                continue
            d = haversine(lat, lon, s_lat, s_lon)
            if best is None or d < best[0]:
                best = (d, code, name)
        except Exception:
            pass
    return best  # (dist_m, CODE, Name)

def build_station_file_url(station_code, granularity="t", period="now"):
    """z.B. .../ch.meteoschweiz.ogd-smn/now/t/ogd-smn_zur_t_now.csv"""
    code = station_code.lower()
    return f"{MCH_BASE}/{MCH_COLLECTION}/{period}/{granularity}/ogd-smn_{code}_{granularity}_{period}.csv"

def parse_latest_rows(rows, n=6):
    valid = [r for r in rows if r and any((v or "").strip() for v in r.values())]
    return valid[-n:] if valid else []

def _to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None

def extract_fields(row):
    """Heuristisch gängige Parameter aus SwissMetNet-CSV ziehen."""
    temp=hum=press=wind_ms=gust_ms=wind_dir=prec_10=None
    press_prio = None  # qff > qfe > rest
    for k, v in row.items():
        kl = k.lower()
        s = (v or "").strip()
        if not s or s in ("-", "NaN"):
            continue
        f = _to_float(s)
        if f is None:
            continue
        if temp is None and ("tre200" in kl or "temp" in kl or kl.startswith("ta")):
            temp = f
        elif hum is None and ("ure200" in kl or "rhu" in kl or "feuchte" in kl or kl == "rh"):
            hum = f
        elif ("qff" in kl) or ("qfe" in kl) or ("druck" in kl) or ("press" in kl):
            pr = 3 if "qff" in kl else (2 if "qfe" in kl else 1)
            if press_prio is None or pr > press_prio:
                press_prio = pr
                press = f
        elif wind_ms is None and ("fu3010" in kl or kl.startswith("ff") or "wind" in kl):
            wind_ms = f
        elif gust_ms is None and ("fx" in kl or "gust" in kl):
            gust_ms = f
        elif wind_dir is None and ("dkl010" in kl or kl.startswith("dd") or "dir" in kl):
            wind_dir = f
        elif prec_10 is None and ("rre" in kl or kl.startswith("rr") or "precip" in kl or "nied" in kl):
            prec_10 = f
    return dict(temp=temp, hum=hum, press=press, wind_ms=wind_ms,
                gust_ms=gust_ms, wind_dir=wind_dir, prec_10=prec_10)

def deg_to_cardinal(d):
    if d is None:
        return None
    dirs = ["N","NNO","NO","ONO","O","OSO","SO","SSO","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    ix = int((d % 360) / 22.5 + 0.5) % 16
    return dirs[ix]

def beaufort(w_kmh):
    if w_kmh is None:
        return None
    thresholds = [1,6,12,20,29,39,50,62,75,89,103,118,1e9]
    for i, thr in enumerate(thresholds):
        if w_kmh <= thr:
            return i
    return 12

def windchill(temp_c, wind_kmh):
    if temp_c is None or wind_kmh is None:
        return None
    if temp_c > 10 or wind_kmh < 4.8:
        return None
    v = wind_kmh
    return 13.12 + 0.6215*temp_c - 11.37*(v**0.16) + 0.3965*temp_c*(v**0.16)

def fmt_utc_local(ts_str):
    # SwissMetNet nutzt UTC 'dd.mm.yyyy HH:MM'
    try:
        dt_utc = datetime.strptime(ts_str, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        return QDateTime.fromSecsSinceEpoch(int(dt_utc.timestamp())).toString("dd.MM. HH:mm")
    except Exception:
        return ts_str or ""


# ───────────────────────────────────────── Backend ─────────────────────────────────────────
class Backend(QObject):
    # Signale für QML
    timeChanged    = Signal(str)
    dateChanged    = Signal(str)
    weatherChanged = Signal(str)
    stationChanged = Signal(str)
    calendarChanged= Signal(str)
    newsChanged    = Signal(str)
    todosChanged   = Signal(str)
    cpuChanged     = Signal(str)
    dimChanged     = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        QLocale.setDefault(QLocale(QLocale.German, QLocale.Switzerland))
        self.cfg = load_config()
        self.lat = float(self.cfg.get("lat", 47.3769))
        self.lon = float(self.cfg.get("lon", 8.5417))
        self.use_mch = bool(self.cfg.get("use_meteoswiss", True))
        self.mch_station_fixed = (self.cfg.get("mch_station") or "").strip().upper()

        # CLOCK
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._emit_time)
        self._clock.start(1000)
        self._emit_time()

        # WEATHER
        self._weather = QTimer(self)
        self._weather.timeout.connect(self.update_weather_async)
        self._weather.start(10 * 60 * 1000)
        self.update_weather_async()

        # CALENDAR
        self._cal = QTimer(self)
        self._cal.timeout.connect(self.update_calendar_async)
        self._cal.start(15 * 60 * 1000)
        self.update_calendar_async()

        # NEWS – Fetch & Rotation
        self.news_items = []
        self.news_idx = 0

        self._news_fetch = QTimer(self)
        self._news_fetch.timeout.connect(self.update_news_async)
        self._news_fetch.start(30 * 60 * 1000)   # alle 30 min neu laden
        self.update_news_async()                  # sofort laden

        self._news_rotate = QTimer(self)
        self._news_rotate.timeout.connect(self.rotate_news)
        self._news_rotate.start(20 * 1000)       # alle 20 s nächste Headline

        # TODOS
        self._todos = QTimer(self)
        self._todos.timeout.connect(self.update_todos)
        self._todos.start(30 * 1000)
        self.update_todos()

        # CPU
        self._cpu = QTimer(self)
        self._cpu.timeout.connect(self.update_cpu)
        self._cpu.start(10 * 1000)
        self.update_cpu()

        # DIMMING
        self._dim = QTimer(self)
        self._dim.timeout.connect(self.update_dimming)
        self._dim.start(5 * 60 * 1000)
        self.update_dimming()

    # ───────── CLOCK ─────────
    def _emit_time(self):
        now = QDateTime.currentDateTime()
        self.timeChanged.emit(now.toString("HH:mm:ss"))
        self.dateChanged.emit(now.toString("dddd, dd. MMMM yyyy"))

    # ───────── WEATHER ─────────
    @Slot()
    def update_weather_async(self):
        threading.Thread(target=self._fetch_weather, daemon=True).start()

    def _fetch_weather(self):
        if self.use_mch:
            try:
                # 1) Station bestimmen
                stations = csv_get(MCH_STATIONS_CSV)
                code = name = None
                if self.mch_station_fixed:
                    for r in stations:
                        if (r.get("Abbr") or r.get("abbr","")).strip().upper() == self.mch_station_fixed:
                            code = self.mch_station_fixed
                            name = (r.get("Name") or r.get("name") or code).strip()
                            break
                if not code:
                    best = nearest_station(self.lat, self.lon, stations)
                    if not best:
                        raise RuntimeError("Keine Station gefunden")
                    _, code, name = best
                self.stationChanged.emit(f"{name} ({code})")

                # 2) 10-Minuten CSV holen (aktuell)
                url = build_station_file_url(code, "t", "now")
                rows = csv_get(url, timeout=8)
                last6 = parse_latest_rows(rows, n=6)
                if not last6:
                    raise RuntimeError("Keine Messwerte")

                last = last6[-1]
                ts   = last.get("ReferenceTS") or last.get("ref_ts") or ""
                when = fmt_utc_local(ts)
                flds = extract_fields(last)

                # Regen 1h aus den letzten 6 Zeilen sum
                rain_1h = 0.0; have_rain = False
                for r in last6:
                    ff = extract_fields(r)
                    if ff["prec_10"] is not None:
                        rain_1h += max(0.0, ff["prec_10"])
                        have_rain = True
                rain_10 = flds["prec_10"] if flds["prec_10"] is not None else None

                # Ableitungen
                wind_kmh = flds["wind_ms"]*3.6 if flds["wind_ms"] is not None else None
                gust_kmh = flds["gust_ms"]*3.6 if flds["gust_ms"] is not None else None
                bft  = beaufort(wind_kmh) if wind_kmh is not None else None
                card = deg_to_cardinal(flds["wind_dir"])
                chill = windchill(flds["temp"], wind_kmh)
                felt = round(chill) if (chill is not None and abs(chill - flds["temp"]) >= 1) else None

                # Anzeige
                line1 = []
                if flds["temp"] is not None:
                    line1.append(f"{flds['temp']:.0f}°C")
                    if felt is not None:
                        line1.append(f"gefühlt {felt:.0f}°C")
                if flds["hum"] is not None:
                    line1.append(f"LF {flds['hum']:.0f}%")
                line1 = " · ".join(line1) if line1 else "—"

                line2 = []
                if wind_kmh is not None:
                    base = f"Wind {wind_kmh:.0f} km/h"
                    if bft is not None:
                        base += f" ({bft} Bft)"
                    if card:
                        base += f" {card}"
                    line2.append(base)
                if gust_kmh is not None:
                    line2.append(f"Böen {gust_kmh:.0f} km/h")
                line2 = " · ".join(line2)

                line3 = ""
                if have_rain or (rain_10 and rain_10 > 0):
                    parts = []
                    if rain_10 is not None:
                        parts.append(f"Regen {rain_10:.1f} mm / 10 min")
                    parts.append(f"∑1 h {rain_1h:.1f} mm")
                    line3 = " · ".join(parts)

                line4 = []
                if flds["press"] is not None:
                    line4.append(f"Druck {flds['press']:.0f} hPa")
                if when:
                    line4.append(when)
                line4 = " · ".join(line4)

                msg = "\n".join([s for s in (line1, line2, line3, line4) if s])
                self.weatherChanged.emit(msg if msg else "Wetter: –")
                return
            except Exception:
                # Fallback – unten Open-Meteo
                pass

        # Fallback: Open-Meteo (kompakt, JSON)
        try:
            params = {
                "latitude": self.lat,
                "longitude": self.lon,
                "current_weather": True,
                "hourly": "relativehumidity_2m,surface_pressure,precipitation",
                "timezone": "auto"
            }
            r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=8)
            r.raise_for_status()
            j = r.json()
            cw = j.get("current_weather", {})
            temp = cw.get("temperature"); wind = cw.get("windspeed"); wd = cw.get("winddirection")
            hum = press = rain_1h = None
            try:
                t_now = cw.get("time")
                idx = j["hourly"]["time"].index(t_now) if t_now in j["hourly"]["time"] else -1
                if idx >= 0:
                    hum = j["hourly"]["relativehumidity_2m"][idx]
                    press = j["hourly"]["surface_pressure"][idx]
                    rain_1h = j["hourly"]["precipitation"][idx]
            except Exception:
                pass
            card = deg_to_cardinal(wd) if wd is not None else None
            bft = beaufort(wind) if wind is not None else None

            line1 = " · ".join([s for s in (
                f"{temp:.0f}°C" if temp is not None else "",
                f"LF {hum:.0f}%" if hum is not None else ""
            ) if s])
            line2 = ""
            if wind is not None:
                line2 = f"Wind {wind:.0f} km/h"
                if bft is not None: line2 += f" ({bft} Bft)"
                if card: line2 += f" {card}"
            line3 = f"∑1 h {rain_1h:.1f} mm" if isinstance(rain_1h, (int, float)) else ""
            self.stationChanged.emit("Open-Meteo")
            self.weatherChanged.emit("\n".join([s for s in (line1, line2, line3) if s]) or "Wetter: –")
        except Exception:
            self.weatherChanged.emit("Wetter: offline")

    # ───────── CALENDAR ─────────
    @Slot()
    def update_calendar_async(self):
        threading.Thread(target=self._fetch_calendar, daemon=True).start()

    def _fetch_calendar(self):
        ics_url = self.cfg.get("ics_url", "")
        if not ics_url:
            self.calendarChanged.emit("Kalender: –")
            return
        if Calendar is None:
            self.calendarChanged.emit("Kalender: Modul 'ics' fehlt")
            return
        try:
            r = requests.get(ics_url, timeout=8)
            r.raise_for_status()
            cal = Calendar(r.text)

            now = datetime.now().astimezone()
            horizon = now + timedelta(days=14)
            nxt = None

            for ev in cal.events:
                try:
                    start = ev.begin.to('local').naive.replace(tzinfo=None)
                except Exception:
                    start = getattr(ev.begin, "datetime", None)
                if not start:
                    continue
                if start >= now.replace(tzinfo=None) and start <= horizon.replace(tzinfo=None):
                    if nxt is None or start < nxt[0]:
                        try:
                            end = ev.end.to('local').naive.replace(tzinfo=None)
                        except Exception:
                            end = getattr(ev, "end", None)
                            end = getattr(end, "datetime", end)
                        nxt = (start, end, (ev.name or "Termin"))

            if not nxt:
                self.calendarChanged.emit("Kalender: keine Termine")
                return

            start, end, title = nxt
            day  = QDateTime.fromSecsSinceEpoch(int(start.timestamp())).toString("ddd, dd.MM.")
            span = QDateTime.fromSecsSinceEpoch(int(start.timestamp())).toString("HH:mm")
            if end:
                span += "–" + QDateTime.fromSecsSinceEpoch(int(end.timestamp())).toString("HH:mm")
            self.calendarChanged.emit(f"{title} · {day} {span}")
        except Exception:
            self.calendarChanged.emit("Kalender: offline")

    # ───────── NEWS (mehrere Feeds + Rotation) ─────────
    @Slot()
    def rotate_news(self):
        if not self.news_items:
            return
        self.news_idx = (self.news_idx + 1) % len(self.news_items)
        title, source = self.news_items[self.news_idx]
        self.newsChanged.emit(f"{title}  ·  {source}" if source else title)

    @Slot()
    def update_news_async(self):
        threading.Thread(target=self._fetch_news, daemon=True).start()

    def _fetch_news(self):
        feeds = self.cfg.get("news_feeds")
        if not (feeds and isinstance(feeds, list)):
            # Fallback auf einzelnes news_rss
            u = self.cfg.get("news_rss", "")
            feeds = [u] if u else []

        if feedparser is None:
            self.news_items = []
            self.newsChanged.emit("News: Modul 'feedparser' fehlt")
            return

        items = []
        seen = set()
        for u in feeds:
            if not u:
                continue
            try:
                resp = requests.get(u, timeout=8, headers={"User-Agent": "SmartMirror/1.0"})
                resp.raise_for_status()
                fd = feedparser.parse(resp.content)

                source = ""
                try:
                    source = (fd.feed.title or "").strip()
                except Exception:
                    pass

                for e in fd.entries[:8]:  # pro Feed max. 8 Headlines
                    title = (getattr(e, "title", "") or "").strip()
                    if not title:
                        continue
                    key = (title, source)
                    if key in seen:
                        continue
                    seen.add(key)
                    # Optional: zu lange Titel kürzen
                    if len(title) > 180:
                        title = title[:177] + "…"
                    items.append((title, source))
            except Exception:
                continue

        if not items:
            self.news_items = []
            self.newsChanged.emit("News: keine Einträge")
            return

        self.news_items = items
        self.news_idx = 0
        t, s = items[0]
        self.newsChanged.emit(f"{t}  ·  {s}" if s else t)

    # ───────── TODOS ─────────
    @Slot()
    def update_todos(self):
        here = os.path.dirname(__file__)
        p = os.path.join(here, "data", "todos.json")
        if not os.path.exists(p):
            self.todosChanged.emit("To-Dos: (keine)")
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = [t for t in data if not t.get("done")]
            if not items:
                self.todosChanged.emit("Alles erledigt ✅")
                return
            self.todosChanged.emit("\n".join([f"• {t.get('title','(ohne Titel)')}" for t in items[:3]]))
        except Exception:
            self.todosChanged.emit("To-Dos: Fehler")

    # ───────── CPU ─────────
    @Slot()
    def update_cpu(self):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                milli = int(f.read().strip())
            self.cpuChanged.emit(f"CPU {milli/1000:.0f}°C")
        except Exception:
            self.cpuChanged.emit("CPU –")

    # ───────── DIMMING ─────────
    def update_dimming(self):
        h = QDateTime.currentDateTime().time().hour()
        self.dimChanged.emit(0.4 if (h >= 22 or h < 6) else 1.0)
