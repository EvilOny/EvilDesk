import sys
import json
import base64
import asyncio
import threading
from io import BytesIO

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QGraphicsBlurEffect, QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, QObject, Signal, QPropertyAnimation, QEasingCurve, Property
from PySide6.QtGui import QPixmap, QIcon, QColor, QLinearGradient, QBrush, QPainter, QFont, QFontDatabase
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile

from PIL import Image
try:
    from colorthief import ColorThief
    _HAS_COLORTHIEF = True
except Exception:
    _HAS_COLORTHIEF = False

import websockets

WS_URL = "ws://127.0.0.1:8765"


class WSBridge(QObject):
    state_received = Signal(dict)
    connected = Signal()
    disconnected = Signal()


bridge = WSBridge()


class MusicPlayer(QMainWindow):
    def __init__(self):
        super().__init__()

        loader = QUiLoader()
        f = QFile("main.ui")
        f.open(QFile.ReadOnly)
        self.ui = loader.load(f)
        f.close()
        self.setCentralWidget(self.ui)

        # ===== background =====
        self.bg_label = QLabel(self.centralWidget())
        self.bg_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_label.lower()
        self.bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(30)
        self.bg_label.setGraphicsEffect(blur)

        # show window
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(800, 480)
        self.show()

        # ===== widgets =====
        self.cover = self.ui.findChild(QLabel, "coverLabel")
        self.track_title = self.ui.findChild(QLabel, "trackTitleLabel")
        self.artist = self.ui.findChild(QLabel, "artistLabel")

        self.prevBtn = self.ui.findChild(QPushButton, "prevBtn")
        self.playBtn = self.ui.findChild(QPushButton, "playBtn")
        self.nextBtn = self.ui.findChild(QPushButton, "nextBtn")

        # connection status
        self.status_label = QLabel(self.centralWidget())
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: rgba(255,255,255,200); font-size:12px;")
        self.status_label.setGeometry(self.width()-200, 8, 190, 20)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.raise_()

        # ===== icons =====
        self.icons = {
            "prev": QIcon("icons/prev.png"),
            "play": QIcon("icons/play.png"),
            "pause": QIcon("icons/pause.png"),
            "next": QIcon("icons/next.png"),
        }
        self.prevBtn.setIcon(self.icons["prev"])
        self.playBtn.setIcon(self.icons["play"])
        self.nextBtn.setIcon(self.icons["next"])
        for btn in (self.prevBtn, self.playBtn, self.nextBtn):
            btn.setIconSize(btn.size())

        # ===== button handlers =====
        self.prevBtn.clicked.connect(lambda: self.send_command("prev"))
        self.playBtn.clicked.connect(lambda: self.send_command("playpause"))
        self.nextBtn.clicked.connect(lambda: self.send_command("next"))

        # ===== cover fade animation =====
        self.cover_effect = QGraphicsOpacityEffect()
        self.cover.setGraphicsEffect(self.cover_effect)
        self.fade_anim = QPropertyAnimation(self.cover_effect, b"opacity")
        self.fade_anim.setDuration(350)
        self.fade_anim.setEasingCurve(QEasingCurve.InOutQuad)

        # ===== background interpolation =====
        self.current_bg_colors = None  # [(r1,g1,b1),(r2,g2,b2)]
        self.bg_anim = QPropertyAnimation(self, b"bg_t")
        self.bg_anim.setDuration(600)
        self.bg_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._bg_t = 0.0

        self.bg_anim.valueChanged.connect(self.update_bg_gradient)

        # ===== connect signals =====
        bridge.state_received.connect(self.update_from_ws)
        bridge.connected.connect(self.on_connected)
        bridge.disconnected.connect(self.on_disconnected)

        # ===== websocket =====
        self._ws = None
        self._ws_loop = None
        self._ws_loop_thread = None
        self.start_ws_thread()
        self.apply_styles()

    # ===== property for animation interpolation =====
    def get_bg_t(self):
        return self._bg_t

    def set_bg_t(self, val):
        self._bg_t = val

    bg_t = Property(float, get_bg_t, set_bg_t)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "bg_label") and self.bg_label:
            self.bg_label.setGeometry(0, 0, self.width(), self.height())
        if hasattr(self, "status_label"):
            self.status_label.setGeometry(self.width()-200, 8, 190, 20)

    def apply_styles(self):
        self.setStyleSheet("""
        QMainWindow { background: #141414; }

        #trackTitleLabel, #artistLabel {
            color: white;
            font-size: 18px;
            font-family: "Montserrat Alternates", "Segoe UI", "Arial Rounded MT", "Roboto", sans-serif;
            font-weight: 500;
        }

        /* Панель управления */
        QFrame#controlPanel { 
            background: rgba(255,255,255,20); 
            border-radius: 18px; 
        }

        /* Кнопки — прозрачные, без подсветки при наведении */
        QPushButton {
            background: transparent;
            border: none;
            padding: 8px;
            border-radius: 14px;
        }
        """)

    def start_ws_thread(self):
        def thread_target():
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            self._ws_loop.run_until_complete(self._ws_main())
        self._ws_loop_thread = threading.Thread(target=thread_target, daemon=True)
        self._ws_loop_thread.start()

    async def _ws_main(self):
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    bridge.connected.emit()
                    async for msg in ws:
                        try:
                            payload = json.loads(msg)
                        except Exception:
                            continue
                        if payload.get("type") == "state":
                            bridge.state_received.emit(payload.get("data", {}))
            except Exception:
                self._ws = None
                bridge.disconnected.emit()
                await asyncio.sleep(2)

    def send_command(self, cmd: str):
        if self._ws is None or self._ws_loop is None:
            return
        async def _send():
            try:
                await self._ws.send(json.dumps({"cmd": cmd}))
            except Exception as e:
                print("WS send error:", e)
        asyncio.run_coroutine_threadsafe(_send(), self._ws_loop)

    def on_connected(self):
        self.status_label.setText("Connected")
        self.status_label.setStyleSheet("color: lightgreen; font-size:12px;")

    def on_disconnected(self):
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: salmon; font-size:12px;")

    # ===== main update =====
    def update_from_ws(self, data: dict):
        try:
            cover_b64 = data.get("cover")
            if cover_b64:
                img_bytes = base64.b64decode(cover_b64)
                qpix = QPixmap()
                qpix.loadFromData(img_bytes)

                # cover animation
                self.fade_anim.stop()
                self.cover_effect.setOpacity(0.0)
                self.cover.setPixmap(qpix)
                self.cover.setScaledContents(True)
                self.fade_anim.setStartValue(0.0)
                self.fade_anim.setEndValue(1.0)
                self.fade_anim.start()

                # background animation
                self.animate_bg_from_bytes(img_bytes)

            self.track_title.setText(data.get("track", "Unknown"))
            self.artist.setText(data.get("artist", "Unknown"))
            if data.get("is_playing"):
                self.playBtn.setIcon(self.icons["pause"])
            else:
                self.playBtn.setIcon(self.icons["play"])
        except Exception as e:
            print("Error updating GUI:", e)

    # ===== interpolate colors =====
    @staticmethod
    def interpolate_color(c1, c2, t):
        r = int(c1[0] + (c2[0]-c1[0])*t)
        g = int(c1[1] + (c2[1]-c1[1])*t)
        b = int(c1[2] + (c2[2]-c1[2])*t)
        return r, g, b

    def animate_bg_from_bytes(self, img_bytes: bytes):
        try:
            if _HAS_COLORTHIEF:
                buf = BytesIO(img_bytes)
                ct = ColorThief(buf)
                r, g, b = ct.get_color(quality=1)
            else:
                img = Image.open(BytesIO(img_bytes)).convert("RGB")
                img = img.resize((60, 60))
                pixels = list(img.getdata())
                r = sum(p[0] for p in pixels) // len(pixels)
                g = sum(p[1] for p in pixels) // len(pixels)
                b = sum(p[2] for p in pixels) // len(pixels)

            top_multiplier = 1.0
            bottom_multiplier = 0.28
            new_colors = [
                (int(min(255, r*top_multiplier)), int(min(255, g*top_multiplier)), int(min(255, b*top_multiplier))),
                (int(r*bottom_multiplier), int(g*bottom_multiplier), int(b*bottom_multiplier))
            ]

            if self.current_bg_colors is None:
                self.current_bg_colors = new_colors
                pix = self.create_gradient_pixmap(new_colors)
                self.bg_label.setPixmap(pix)
                return

            self.old_bg_colors = self.current_bg_colors
            self.new_bg_colors = new_colors
            self.bg_anim.stop()
            self.bg_anim.setStartValue(0.0)
            self.bg_anim.setEndValue(1.0)
            self.bg_anim.start()

        except Exception as e:
            print("animate_bg_from_bytes error:", e)

    def update_bg_gradient(self, t):
        c_top = self.interpolate_color(self.old_bg_colors[0], self.new_bg_colors[0], t)
        c_bottom = self.interpolate_color(self.old_bg_colors[1], self.new_bg_colors[1], t)
        pix = self.create_gradient_pixmap([c_top, c_bottom])
        self.bg_label.setPixmap(pix)
        if t >= 1.0:
            self.current_bg_colors = self.new_bg_colors

    def create_gradient_pixmap(self, colors):
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.4, QColor(*colors[0], 230))
        grad.setColorAt(1, QColor(*colors[1], 200))
        pix = QPixmap(self.bg_label.size())
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.fillRect(pix.rect(), QBrush(grad))
        painter.end()
        return pix


if __name__ == "__main__":
    app = QApplication(sys.argv)

    font_id = QFontDatabase.addApplicationFont("fonts/MontserratAlternates-Medium.ttf")
    if font_id != -1:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            MONTSERRAT_ALT = families[0]
        else:
            MONTSERRAT_ALT = "Sans Serif"
    else:
        MONTSERRAT_ALT = "Sans Serif"

    w = MusicPlayer()
    sys.exit(app.exec())
