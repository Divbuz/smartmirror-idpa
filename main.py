import os, sys
from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtCore import QUrl, Qt
from PySide6.QtQml import QQmlApplicationEngine
from backend import Backend

def url_here(path):
    return QUrl.fromLocalFile(os.path.join(os.path.dirname(__file__), path))

if __name__ == "__main__":
    # Erstellen der Hauptanwendung. QGuiApplication wird genutzt, da wir QML verwenden
    app = QGuiApplication(sys.argv)
    app.setOverrideCursor(QCursor(Qt.BlankCursor))  # Mauszeiger ausschalten (damit er nicht über unserem UI sichtbar ist)
    engine = QQmlApplicationEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(url_here("qml/main.qml"))
    if not engine.rootObjects():
        sys.exit(1)

    # Vollbild (frameless) erzwingen
    root = engine.rootObjects()[0]
    if hasattr(root, "showFullScreen"):
        root.showFullScreen()
# Starten des Event-Loops der Anwendung
    sys.exit(app.exec())