import QtQuick
import QtQuick.Controls
import QtQuick.Window

Window {
    id: win
    visible: true
    width: Screen.width
    height: Screen.height
    color: "black"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint

    // Nacht-Dimmung
    property real dimmer: 1.0
    opacity: dimmer
    Behavior on opacity { NumberAnimation { duration: 400 } }

    // ───────── Zentrum: Uhr & Datum ─────────
    Column {
        anchors.centerIn: parent
        spacing: 12

        Text {
            id: timeTxt
            text: "--:--:--"
            font.pixelSize: 160
            font.family: "DejaVu Sans"
            color: "#ECECEC"
            horizontalAlignment: Text.AlignHCenter
        }

        Text {
            id: dateTxt
            text: ""
            font.pixelSize: 36
            font.family: "DejaVu Sans"
            color: "#BEBEBE"
            horizontalAlignment: Text.AlignHCenter
        }
    }

    // ───────── Links oben: Wetter + Station (OHNE CPU) ─────────
    Column {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 48
        spacing: 6

        Text {
            id: weatherTxt
            text: "Wetter …"
            width: 640
            wrapMode: Text.Wrap
            font.pixelSize: 46  // Etwas größer für bessere Lesbarkeit
            font.family: "DejaVu Sans"
            color: "#AFAFAF"
        }
        Text {
            id: stationTxt
            text: ""
            font.pixelSize: 16
            font.family: "DejaVu Sans"
            color: "#7F7F7F"
        }
    }

    // ───────── Rechts oben: Kalender ─────────
    Rectangle {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 48
        color: "transparent"
        width: 760
        height: 600

        Text {
            id: calTxt
            text: "Lade Kalender..."
            width: parent.width
            wrapMode: Text.Wrap
            font.pixelSize: 28
            font.family: "DejaVu Sans"
            color: "#AFAFAF"
            lineHeight: 1.3
        }
    }

    // ───────── Unten: NEWS-TICKER (Dein verbesserter Code) ─────────
    Rectangle {
        id: newsBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom

        property int fontPx: 24
        property int vpad: 10
        height: Math.max(72, fontPx + vpad*2 + 8)

        color: "transparent"

        // Hintergrund
        Rectangle { anchors.fill: parent; color: "#000"; opacity: 0.18 }

        Item {
            id: clipper
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.leftMargin: 24
            anchors.rightMargin: 24
            anchors.topMargin: newsBar.vpad
            anchors.bottomMargin: newsBar.vpad
            clip: true

            Text {
                id: newsText
                text: "Lade Nachrichten..."
                anchors.verticalCenter: parent.verticalCenter
                x: 0
                font.pixelSize: newsBar.fontPx
                font.family: "DejaVu Sans"
                renderType: Text.NativeRendering
                color: "#E0E0E0"
                opacity: 1.0
                elide: Text.ElideNone
            }
        }

        // Marquee-Animation
        NumberAnimation {
            id: marquee
            target: newsText
            property: "x"
            loops: Animation.Infinite
            easing.type: Easing.Linear
            running: false
        }

        // Animationen
        PropertyAnimation { id: fadeOut; target: newsText; property: "opacity"; from: 1.0; to: 0.0; duration: 220; running: false }
        PropertyAnimation { id: fadeIn;  target: newsText; property: "opacity"; from: 0.0; to: 1.0; duration: 240; running: false }

        Timer {
            id: measureTimer
            interval: 50
            repeat: false
            onTriggered: {
                marquee.stop()
                startDelay.restart()
            }
        }

        Timer {
            id: startDelay
            interval: 700
            repeat: false
            onTriggered: {
                if (newsText.paintedWidth > clipper.width) {
                    newsText.x = clipper.width
                    var dist = newsText.paintedWidth + clipper.width
                    var pxPerSec = 120
                    marquee.from = clipper.width
                    marquee.to = -newsText.paintedWidth
                    marquee.duration = Math.max(4000, (dist / pxPerSec) * 1000)
                    marquee.running = true
                } else {
                    newsText.x = Math.floor((clipper.width - newsText.paintedWidth) / 2)
                }
                fadeIn.restart()
            }
        }

        property string pendingNews: ""
        function setNewsSmooth(s) {
            pendingNews = s
            marquee.stop()
            if (newsText.opacity > 0.05) fadeOut.restart()
            else applyPending()
        }
        function applyPending() {
            newsText.text = pendingNews
            measureTimer.restart()
        }
        Connections {
            target: fadeOut
            function onStopped() { newsBar.applyPending() }
        }
    }

    // ───────── Backend-Verbindung ─────────
    Connections {
        target: backend
        
        function onTimeChanged(s)      { timeTxt.text = s }
        function onDateChanged(s)      { dateTxt.text = s }
        function onWeatherChanged(s)   { weatherTxt.text = s }
        function onStationChanged(s)   { stationTxt.text = s }
        
        // CPU wurde hier entfernt!
        
        function onCalendarChanged(s)  { calTxt.text = s }
        function onNewsChanged(s)      { newsBar.setNewsSmooth(s) }
        function onDimChanged(v)       { win.dimmer = v }
    }
}