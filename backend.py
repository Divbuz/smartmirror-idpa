# backend.py
import os
import csv
import math
import json
import threading
import requests
from datetime import datetime, timedelta, timezone

# Qt
from PySide6.QtCore import QObject, Signal, Slot, QTimer, QDateTime, QLocale

# --- Konfiguration ---
# Hier den Link reinpacken (nicht in public repos pushen!)
ICAL_URL_FIXED = "" 

# Libs importieren, aber nicht crashen wenn sie fehlen
try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from ics import Calendar
except ImportError:
    Calendar = None


# --- MeteoSwiss Konstanten & Helper ---
MCH_BASE = "https://data.geo.admin.ch"
MCH_COLLECTION = "ch.meteoschweiz.ogd-smn"
MCH_STATIONS_CSV = f"{MCH_BASE}/{MCH_COLLECTION}/ogd-smn_meta_stations.csv"

def load_config():
    # Versucht config.json zu laden, sonst hardcoded defaults
    here = os.path.dirname(__file__)
    path = os.path.join(here, "data", "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass # Datei kaputt oder leer? Egal, weiter mit Defaults.
    
    return {
        "lat": 47.3769,  # Zürich
        "lon": 8.5417,
        "ics_url": ICAL_URL_FIXED,
        "use_meteoswiss": True,
        "mch_station": "",
        "news_feeds": [
            "https://www.tagesschau.de/index~rss2.xml",
            "https://feeds.bbci.co.uk/news/rss.xml"
        ]
    }

def csv_get(url, timeout=10):
    # MeteoSwiss liefert CP1252 codierte CSVs mit Semikolon
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "SmartMirror/1.0"})
        r.raise_for_status()
        text = r.content.decode("cp1252", errors="replace")
        return list(csv.DictReader(text.splitlines(), delimiter=";"))
    except Exception as e:
        print(f"CSV Error ({url}): {e}")
        return []

def haversine(lat1, lon1, lat2, lon2):
    # Abstand zwischen zwei Koordinaten berechnen
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
            # Daten parsen, manchmal sind Felder leer/nan
            s_lat = float(r.get("Latitude", r.get("latitude", "nan")))
            s_lon = float(r.get("Longitude", r.get("longitude", "nan")))
            code = (r.get("Abbr") or r.get("abbr", "")).strip()
            name = (r.get("Name") or r.get("name", code)).strip()
            
            if not code or math.isnan(s_lat) or math.isnan(s_lon): continue
            
            dist = haversine(lat, lon, s_lat, s_lon)
            if best is None or dist < best[0]:
                best = (dist, code, name)
        except Exception: pass
    return best

def build_station_file_url(station_code, granularity="t", period="now"):
    code = station_code.lower()
    return f"{MCH_BASE}/{MCH_COLLECTION}/{period}/{granularity}/ogd-smn_{code}_{granularity}_{period}.csv"

def parse_latest_rows(rows, n=6):
    # Nur Zeilen mit Inhalt nehmen
    valid = [r for r in rows if r and any((v or "").strip() for v in r.values())]
    return valid[-n:] if valid else []

def _to_float(v):
    try: return float(str(v).replace(",", "."))
    except: return None

def extract_fields(row):
    # Versucht, die Daten aus dem CSV Chaos zu extrahieren.
    # Die Spaltennamen ändern sich manchmal, daher die vielen 'or' checks.
    data = {"temp": None, "hum": None, "wind_ms": None, "gust_ms": None, "prec_10": None, "press": None, "wind_dir": None}
    press_prio = None

    for k, v in row.items():
        kl = k.lower(); s = (v or "").strip()
        if not s or s in ("-", "NaN"): continue
        f = _to_float(s)
        if f is None: continue
        
        if data["temp"] is None and ("tre200" in kl or "temp" in kl or kl.startswith("ta")): data["temp"] = f
        elif data["hum"] is None and ("ure200" in kl or "rhu" in kl): data["hum"] = f
        elif ("qff" in kl) or ("qfe" in kl) or ("press" in kl): # Druck Priorität: QFF > QFE
            pr = 3 if "qff" in kl else (2 if "qfe" in kl else 1)
            if press_prio is None or pr > press_prio:
                press_prio = pr; data["press"] = f
        elif data["wind_ms"] is None and ("fu3010" in kl or "wind" in kl): data["wind_ms"] = f
        elif data["gust_ms"] is None and ("fx" in kl or "gust" in kl): data["gust_ms"] = f
        elif data["wind_dir"] is None and ("dkl010" in kl or "dir" in kl): data["wind_dir"] = f
        elif data["prec_10"] is None and ("rre" in kl or "precip" in kl): data["prec_10"] = f

    return data

def deg_to_cardinal(d):
    if d is None: return None
    dirs = ["N","NNO","NO","ONO","O","OSO","SO","SSO","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[int((d % 360) / 22.5 + 0.5) % 16]

def beaufort(w_kmh):
    if w_kmh is None: return None
    thresholds = [1,6,12,20,29,39,50,62,75,89,103,118,1e9]
    for i, thr in enumerate(thresholds):
        if w_kmh <= thr: return i
    return 12

def windchill(temp_c, wind_kmh):
    # Formel gilt nur unter 10°C und über 5km/h Wind
    if temp_c is None or wind_kmh is None or temp_c > 10 or wind_kmh < 4.8: return None
    v = wind_kmh
    return 13.12 + 0.6215*temp_c - 11.37*(v**0.16) + 0.3965*temp_c*(v**0.16)

def fmt_utc_local(ts_str):
    try:
        dt_utc = datetime.strptime(ts_str, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        return QDateTime.fromSecsSinceEpoch(int(dt_utc.timestamp())).toString("dd.MM. HH:mm")
    except: return ts_str or ""


# --- Main Class ---
class Backend(QObject):
    # Signale für QML
    timeChanged     = Signal(str)
    dateChanged     = Signal(str)
    weatherChanged  = Signal(str)
    stationChanged  = Signal(str)
    calendarChanged = Signal(str)
    newsChanged     = Signal(str)
    dimChanged      = Signal(float)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Schweizer Zeit für korrekte Wochentage/Datumsformate
        QLocale.setDefault(QLocale(QLocale.German, QLocale.Switzerland))
        
        self.cfg = load_config()
        self.lat = float(self.cfg.get("lat", 47.3769))
        self.lon = float(self.cfg.get("lon", 8.5417))
        self.use_mch = bool(self.cfg.get("use_meteoswiss", True))
        self.mch_station_fixed = (self.cfg.get("mch_station") or "").strip().upper()

        # --- Timer Setup ---
        
        # Uhr (1s)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._emit_time)
        self._clock.start(1000)
        self._emit_time()

        # Wetter (10min)
        self._weather = QTimer(self)
        self._weather.timeout.connect(self.update_weather_async)
        self._weather.start(10 * 60 * 1000)
        self.update_weather_async()

        # Kalender (15min)
        self._cal = QTimer(self)
        self._cal.timeout.connect(self.update_calendar_async)
        self._cal.start(15 * 60 * 1000)
        self.update_calendar_async()

        # News Fetch (30min) & Rotate (20s)
        self.news_items = []
        self.news_idx = 0
        self._news_fetch = QTimer(self)
        self._news_fetch.timeout.connect(self.update_news_async)
        self._news_fetch.start(30 * 60 * 1000)
        self.update_news_async()

        self._news_rotate = QTimer(self)
        self._news_rotate.timeout.connect(self.rotate_news)
        self._news_rotate.start(20 * 1000)

        # Dimm-Check (5min)
        self._dim = QTimer(self)
        self._dim.timeout.connect(self.update_dimming)
        self._dim.start(5 * 60 * 1000)
        self.update_dimming()

    def _emit_time(self):
        now = QDateTime.currentDateTime()
        self.timeChanged.emit(now.toString("HH:mm:ss"))
        self.dateChanged.emit(now.toString("dddd, dd. MMMM yyyy"))

    # --- Wetter ---
    @Slot()
    def update_weather_async(self):
        # API Calls im Thread, sonst ruckelt die UI
        threading.Thread(target=self._fetch_weather, daemon=True).start()

    def _fetch_weather(self):
        # 1. MeteoSwiss Hauptquelle
        if self.use_mch:
            try:
                stations = csv_get(MCH_STATIONS_CSV)
                code = name = None
                
                # Check ob fixe Station in Config
                if self.mch_station_fixed:
                    for r in stations:
                        if (r.get("Abbr") or "").strip().upper() == self.mch_station_fixed:
                            code = self.mch_station_fixed
                            name = (r.get("Name") or code).strip()
                            break
                
                # Sonst nächste suchen
                if not code:
                    best = nearest_station(self.lat, self.lon, stations)
                    if not best: raise RuntimeError("Keine Station gefunden")
                    _, code, name = best
                
                self.stationChanged.emit(f"{name} ({code})")

                # Daten holen
                url = build_station_file_url(code, "t", "now")
                rows = csv_get(url, timeout=8)
                last6 = parse_latest_rows(rows, n=6)
                
                if not last6: raise RuntimeError("CSV leer")

                last = last6[-1]
                when = fmt_utc_local(last.get("ReferenceTS"))
                flds = extract_fields(last)

                # Regen akkumulieren (letzte 60min)
                rain_1h = 0.0
                have_rain = False
                for r in last6:
                    ff = extract_fields(r)
                    if ff["prec_10"] is not None:
                        rain_1h += max(0.0, ff["prec_10"])
                        have_rain = True
                
                # Berechnungen für Anzeige
                wind_kmh = flds["wind_ms"] * 3.6 if flds["wind_ms"] else None
                gust_kmh = flds["gust_ms"] * 3.6 if flds["gust_ms"] else None
                bft = beaufort(wind_kmh)
                felt = round(windchill(flds["temp"], wind_kmh)) if windchill(flds["temp"], wind_kmh) else None

                # Text zusammenbauen
                lines = []
                
                # Zeile 1: Temp
                l1 = f"{flds['temp']:.0f}°C" if flds["temp"] is not None else ""
                if felt: l1 += f" (gefühlt {felt:.0f}°C)"
                if flds["hum"]: l1 += f" · LF {flds['hum']:.0f}%"
                if l1: lines.append(l1)

                # Zeile 2: Wind
                l2 = ""
                if wind_kmh: l2 += f"Wind {wind_kmh:.0f} km/h"
                if bft: l2 += f" ({bft} Bft)"
                if gust_kmh: l2 += f" · Böen {gust_kmh:.0f}"
                if l2: lines.append(l2)

                # Zeile 3: Regen
                if have_rain or (flds["prec_10"] and flds["prec_10"] > 0):
                    lines.append(f"Regen 1h: {rain_1h:.1f} mm")
                
                # Zeile 4: Update Zeit
                if when: lines.append(when)

                self.weatherChanged.emit("\n".join(lines) if lines else "Wetter: –")
                return # Erfolg -> Raus hier
            
            except Exception as e:
                print(f"MeteoSwiss Fail: {e}")
                # Fallback läuft weiter unten

        # 2. Open-Meteo Fallback
        try:
            params = {
                "latitude": self.lat, "longitude": self.lon,
                "current_weather": True, "timezone": "auto"
            }
            r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=5)
            cw = r.json().get("current_weather", {})
            
            self.stationChanged.emit("Open-Meteo")
            txt = f"{cw.get('temperature')}°C · Wind {cw.get('windspeed')} km/h"
            self.weatherChanged.emit(txt)
        except:
            self.weatherChanged.emit("Wetter: Offline")

    # --- Kalender ---
    @Slot()
    def update_calendar_async(self):
        threading.Thread(target=self._fetch_calendar, daemon=True).start()

    def _fetch_calendar(self):
        # Link holen
        url = self.cfg.get("ics_url") or ICAL_URL_FIXED
        
        if not url:
            self.calendarChanged.emit("Kalender: Link fehlt")
            return
        
        if Calendar is None:
            self.calendarChanged.emit("Modul 'ics' fehlt")
            return

        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            cal = Calendar(r.text)

            now = datetime.now().astimezone()
            horizon = now + timedelta(days=14)
            events = []

            for ev in cal.events:
                # Startzeit robust ermitteln
                try: start = ev.begin.to('local').datetime
                except: start = getattr(ev.begin, "datetime", None)
                
                if not start: continue
                
                # Nur Zukunft (max 14 Tage)
                if start > (now - timedelta(hours=6)) and start < horizon:
                    all_day = getattr(ev, "all_day", False)
                    # Hack: Wenn keine Uhrzeit da ist, ist es ganztägig
                    if not hasattr(start, 'hour'):
                        all_day = True
                        start = start.replace(hour=0, minute=0, second=0, tzinfo=now.tzinfo)

                    events.append((start, ev.name, all_day))

            # Sortieren
            events.sort(key=lambda x: x[0])

            if not events:
                self.calendarChanged.emit("Keine Termine")
                return

            # Liste bauen (max 6)
            lines = []
            for dt, title, all_day in events[:6]:
                if dt.date() == now.date(): d_str = "Heute"
                elif dt.date() == (now + timedelta(days=1)).date(): d_str = "Morgen"
                else: d_str = dt.strftime("%d.%m.")
                
                if all_day:
                    lines.append(f"• {d_str} : {title}")
                else:
                    lines.append(f"• {d_str} {dt.strftime('%H:%M')} : {title}")

            self.calendarChanged.emit("\n".join(lines))

        except Exception as e:
            print(f"Kalender Error: {e}")
            self.calendarChanged.emit("Kalender: Offline")

    # --- News ---
    @Slot()
    def rotate_news(self):
        if not self.news_items: return
        self.news_idx = (self.news_idx + 1) % len(self.news_items)
        t, s = self.news_items[self.news_idx]
        self.newsChanged.emit(f"{t}  ·  {s}" if s else t)

    @Slot()
    def update_news_async(self):
        threading.Thread(target=self._fetch_news, daemon=True).start()

    def _fetch_news(self):
        feeds = self.cfg.get("news_feeds", [])
        if feedparser is None:
            self.newsChanged.emit("Modul 'feedparser' fehlt"); return

        items = []
        seen = set()

        for u in feeds:
            if not u: continue
            try:
                resp = requests.get(u, timeout=8, headers={"User-Agent": "SmartMirror/1.0"})
                fd = feedparser.parse(resp.content)
                src = (fd.feed.title or "").strip()

                for e in fd.entries[:5]: # Max 5 pro Feed
                    title = (getattr(e, "title", "") or "").strip()
                    if not title: continue
                    
                    # Doppelte filtern
                    key = (title, src)
                    if key in seen: continue
                    seen.add(key)
                    
                    if len(title) > 180: title = title[:177] + "…"
                    items.append((title, src))
            except: continue

        if items:
            self.news_items = items
            self.news_idx = 0
            t, s = items[0]
            self.newsChanged.emit(f"{t}  ·  {s}" if s else t)
        else:
            self.newsChanged.emit("Keine Nachrichten")

    # --- Dimmung ---
    def update_dimming(self):
        h = QDateTime.currentDateTime().time().hour()
        # Nachts abdunkeln
        if h >= 22 or h < 6:
            self.dimChanged.emit(0.4)
        else:
            self.dimChanged.emit(1.0)