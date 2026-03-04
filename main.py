import sys
import json
import asyncio
import threading
from io import BytesIO

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton,
    QGraphicsBlurEffect, QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect, QFrame
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QPropertyAnimation,
    QEasingCurve, Property, QTimer
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor,
    QLinearGradient, QRadialGradient,
    QBrush, QPainter, QFontDatabase
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile

from PIL import Image
try:
    from colorthief import ColorThief
    _HAS_COLORTHIEF = True
except Exception:
    _HAS_COLORTHIEF = False

import websockets
import requests


# ================= BRIDGE =================

class WSBridge(QObject):
    state_received = Signal(dict)
    cover_loaded = Signal(bytes)


bridge = WSBridge()


# ================= PLAYER =================

class MusicPlayer(QMainWindow):

    def __init__(self):
        super().__init__()

        loader = QUiLoader()
        f = QFile("main.ui")
        f.open(QFile.ReadOnly)
        self.ui = loader.load(f)
        f.close()
        self.setCentralWidget(self.ui)

        self.resize(800, 480)
        self.setWindowFlags(Qt.FramelessWindowHint)

        self._clients = set()
        self._ws_loop = None

        self.current_cover = ""
        self.current_bg_colors = None
        self.old_bg_colors = None
        self.new_bg_colors = None
        self._bg_t = 0.0

        self.is_playing = False
        self.is_liked = False

        self.init_ui()
        self.init_background()
        self.init_ws()
        self.init_polling()
        self.apply_styles()
        self.create_edge_shadow()

        self.show()

    # ================= UI =================

    def init_ui(self):

        self.cover = self.ui.findChild(QLabel, "coverLabel")
        self.track_title = self.ui.findChild(QLabel, "trackTitleLabel")
        self.artist = self.ui.findChild(QLabel, "artistLabel")

        # === Text fade effects ===
        self.track_effect = QGraphicsOpacityEffect()
        self.artist_effect = QGraphicsOpacityEffect()

        self.track_title.setGraphicsEffect(self.track_effect)
        self.artist.setGraphicsEffect(self.artist_effect)

        self.track_anim = QPropertyAnimation(self.track_effect, b"opacity")
        self.track_anim.setDuration(300)

        self.artist_anim = QPropertyAnimation(self.artist_effect, b"opacity")
        self.artist_anim.setDuration(300)

        self.controlPanel = self.ui.findChild(QFrame, "controlPanel")

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(60)
        shadow.setColor(QColor(0, 0, 0, 200))
        shadow.setOffset(0, 12)
        self.controlPanel.setGraphicsEffect(shadow)

        self.likeBtn = self.ui.findChild(QPushButton, "likeBtn")
        self.volUpBtn = self.ui.findChild(QPushButton, "volUpBtn")
        self.volDownBtn = self.ui.findChild(QPushButton, "volDownBtn")
        self.prevBtn = self.ui.findChild(QPushButton, "prevBtn")
        self.playBtn = self.ui.findChild(QPushButton, "playBtn")
        self.nextBtn = self.ui.findChild(QPushButton, "nextBtn")

        self.icons = {
            "play": QIcon("icons/play.png"),
            "pause": QIcon("icons/pause.png"),
            "next": QIcon("icons/next.png"),
            "prev": QIcon("icons/prev.png"),
            "like": QIcon("icons/like.png"),
            "like_active": QIcon("icons/like_active.png"),
            "vol_up": QIcon("icons/plus.png"),
            "vol_down": QIcon("icons/minus.png"),
        }

        self.playBtn.setIcon(self.icons["play"])
        self.likeBtn.setIcon(self.icons["like"])
        self.prevBtn.setIcon(self.icons["prev"])
        self.nextBtn.setIcon(self.icons["next"])
        self.volUpBtn.setIcon(self.icons["vol_up"])
        self.volDownBtn.setIcon(self.icons["vol_down"])

        for btn in [
            self.playBtn, self.likeBtn,
            self.prevBtn, self.nextBtn,
            self.volUpBtn, self.volDownBtn
        ]:
            btn.setIconSize(btn.size())

        self.prevBtn.clicked.connect(lambda: self.track_change(-1))
        self.nextBtn.clicked.connect(lambda: self.track_change(1))

        self.playBtn.clicked.connect(self.play_clicked)
        self.likeBtn.clicked.connect(self.like_clicked)

        self.volUpBtn.clicked.connect(
            lambda: self.send_command({"request": "volume", "message": 0.05, "how": 1})
        )
        self.volDownBtn.clicked.connect(
            lambda: self.send_command({"request": "volume", "message": 0.05, "how": -1})
        )

        self.cover_effect = QGraphicsOpacityEffect()
        self.cover.setGraphicsEffect(self.cover_effect)

        self.fade_anim = QPropertyAnimation(self.cover_effect, b"opacity")
        self.fade_anim.setDuration(300)
        self.fade_anim.setEasingCurve(QEasingCurve.InOutQuad)

        bridge.state_received.connect(self.update_from_ws)
        bridge.cover_loaded.connect(self.apply_cover)

    # ================= OPTIMISTIC UI =================

    def play_clicked(self):
        self.is_playing = not self.is_playing
        self.update_play_icon()
        self.send_command({"request": "playerInteraction"})
        QTimer.singleShot(250, self.force_refresh)

    def like_clicked(self):
        self.is_liked = not self.is_liked
        self.update_like_icon()
        self.send_command({"request": "likeInteraction"})
        QTimer.singleShot(250, self.force_refresh)

    def update_play_icon(self):
        self.playBtn.setIcon(
            self.icons["pause"] if self.is_playing else self.icons["play"]
        )

    def update_like_icon(self):
        self.likeBtn.setIcon(
            self.icons["like_active"] if self.is_liked else self.icons["like"]
        )

    # ================= POLLING =================

    def init_polling(self):
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.force_refresh)
        self.poll_timer.start(1500)

    def force_refresh(self):
        for r in ["coverImage", "playingState", "likeState", "trackInfo"]:
            self.send_command({"request": r})

    def track_change(self, delta):
        self.send_command({"request": "track", "message": delta})
        QTimer.singleShot(300, self.force_refresh)

    # ================= UPDATE FROM WS =================

    def update_from_ws(self, data):

        req = data.get("request")
        resp = data.get("response")

        if req == "playingState":
            self.is_playing = (resp == 0)
            self.update_play_icon()

        elif req == "likeState":
            self.is_liked = (resp == 1)
            self.update_like_icon()

        elif req == "trackInfo":

            try:
                track, artist = resp.split(";;")
            except:
                return

            self.fade_text_change(
                self.track_title,
                self.track_effect,
                self.track_anim,
                track or "-"
            )

            self.fade_text_change(
                self.artist,
                self.artist_effect,
                self.artist_anim,
                artist or "-"
            )


        elif req == "coverImage":
            if resp != self.current_cover:
                self.current_cover = resp
                threading.Thread(
                    target=self.load_cover,
                    args=(resp,),
                    daemon=True
                ).start()

    # ================= STYLES =================

    def apply_styles(self):

        self.setStyleSheet("""
        QMainWindow { background: #0f0f0f; }

        #trackTitleLabel, #artistLabel {
            color: white;
            font-size: 20px;
            font-family: "Montserrat Alternates";
        }

        QFrame#controlPanel {
            background: rgba(255,255,255,25);
            border-radius: 22px;
        }

        QPushButton {
            background: transparent;
            border: none;
        }

        QPushButton:focus { outline: none; }
        """)

    # ================= EDGE SHADOW =================

    def create_edge_shadow(self):

        self.edge_overlay = QLabel(self)
        self.edge_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.edge_overlay.raise_()
        self.update_edge_shadow()

    def update_edge_shadow(self):

        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)

        painter = QPainter(pix)

        grad = QRadialGradient(
            self.width()/2,
            self.height()/2,
            max(self.width(), self.height())/1.1
        )
        grad.setColorAt(0.7, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 220))

        painter.fillRect(pix.rect(), QBrush(grad))
        painter.end()

        self.edge_overlay.setPixmap(pix)
        self.edge_overlay.setGeometry(0, 0, self.width(), self.height())

    # ================= BACKGROUND =================

    def init_background(self):

        self.bg_label = QLabel(self.centralWidget())
        self.bg_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_label.lower()

        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(100)
        self.bg_label.setGraphicsEffect(blur)

        self.bg_anim = QPropertyAnimation(self, b"bg_t")
        self.bg_anim.setDuration(600)
        self.bg_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.bg_anim.valueChanged.connect(self.update_bg_gradient)

    def get_bg_t(self): return self._bg_t
    def set_bg_t(self, v): self._bg_t = v
    bg_t = Property(float, get_bg_t, set_bg_t)

    def interpolate(self, a, b, t):
        if t is None:
            return a
        t = float(t)
        return tuple(
            int(a[i] + (b[i] - a[i]) * t)
            for i in range(3)
        )

    def update_bg_gradient(self, t):

        if t is None:
            return

        if not self.old_bg_colors or not self.new_bg_colors:
            return

        t = float(t)

        c1 = self.interpolate(
            self.old_bg_colors[0],
            self.new_bg_colors[0],
            t
        )

        c2 = self.interpolate(
            self.old_bg_colors[1],
            self.new_bg_colors[1],
            t
        )

        self.bg_label.setPixmap(
            self.create_gradient([c1, c2])
        )

        if t >= 1.0:
            self.current_bg_colors = self.new_bg_colors

    def animate_bg(self, img_bytes):

        if _HAS_COLORTHIEF:
            ct = ColorThief(BytesIO(img_bytes))
            r, g, b = ct.get_color(quality=1)
        else:
            img = Image.open(BytesIO(img_bytes)).convert("RGB")
            img = img.resize((50, 50))
            pixels = list(img.getdata())
            r = sum(p[0] for p in pixels)//len(pixels)
            g = sum(p[1] for p in pixels)//len(pixels)
            b = sum(p[2] for p in pixels)//len(pixels)

        new = [(r,g,b),(r//3,g//3,b//3)]

        if self.current_bg_colors is None:
            self.current_bg_colors = new
            self.bg_label.setPixmap(self.create_gradient(new))
            return

        self.old_bg_colors = self.current_bg_colors
        self.new_bg_colors = new

        self.bg_anim.stop()
        self.bg_anim.setStartValue(0)
        self.bg_anim.setEndValue(1)
        self.bg_anim.start()

    def create_gradient(self, colors):

        grad = QLinearGradient(0,0,0,self.height())
        grad.setColorAt(0.4, QColor(*colors[0],230))
        grad.setColorAt(1, QColor(*colors[1],200))

        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)

        p = QPainter(pix)
        p.fillRect(pix.rect(), QBrush(grad))
        p.end()

        return pix

    # ================= WS =================

    def init_polling(self):
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.force_refresh)
        self.poll_timer.start(2000)

    def force_refresh(self):
        for r in ["coverImage","playingState","likeState","trackInfo"]:
            self.send_command({"request": r})

    def track_change(self, delta):
        self.send_command({"request":"track","message":delta})
        QTimer.singleShot(300,self.force_refresh)

    def init_ws(self):
        threading.Thread(target=self.start_ws, daemon=True).start()

    def start_ws(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        loop.run_until_complete(self.start_server())

    async def start_server(self):
        async def handler(ws):
            self._clients.add(ws)
            try:
                async for msg in ws:
                    bridge.state_received.emit(json.loads(msg))
            finally:
                self._clients.remove(ws)

        async with websockets.serve(handler,"0.0.0.0",8765):
            await asyncio.Future()

    def send_command(self,payload):
        if not self._ws_loop or not self._clients:
            return
        async def send():
            for ws in list(self._clients):
                await ws.send(json.dumps(payload))
        asyncio.run_coroutine_threadsafe(send(),self._ws_loop)

    # ================= UPDATE =================

    def load_cover(self,url):
        try:
            r=requests.get(url,timeout=2)
            r.raise_for_status()
            bridge.cover_loaded.emit(r.content)
        except Exception:
            pass

    def apply_cover(self, img_bytes):

        qpix = QPixmap()
        if not qpix.loadFromData(img_bytes):
            return

        new_pix = qpix.scaled(
            self.cover.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )

        if self.cover.pixmap() is None:
            self.cover_effect.setOpacity(0)
            self.cover.setPixmap(new_pix)

            self.fade_anim.stop()
            self.fade_anim.setStartValue(0)
            self.fade_anim.setEndValue(1)
            self.fade_anim.start()

            self.animate_bg(img_bytes)
            return

        self.fade_anim.stop()
        self.fade_anim.setStartValue(1)
        self.fade_anim.setEndValue(0)

        def on_fade_out_finished():

            self.cover.setPixmap(new_pix)

            self.fade_anim.finished.disconnect(on_fade_out_finished)

            self.fade_anim.setStartValue(0)
            self.fade_anim.setEndValue(1)
            self.fade_anim.start()

            self.animate_bg(img_bytes)

        self.fade_anim.finished.connect(on_fade_out_finished)
        self.fade_anim.start()

    def resizeEvent(self,e):
        super().resizeEvent(e)
        self.bg_label.setGeometry(0,0,self.width(),self.height())
        self.update_edge_shadow()

    def fade_text_change(self, label, effect, animation, new_text):

        if label.text() == new_text:
            return

        animation.stop()

        animation.setStartValue(1)
        animation.setEndValue(0)

        def on_fade_out():
            try:
                animation.finished.disconnect(on_fade_out)
            except:
                pass

            label.setText(new_text)

            animation.setStartValue(0)
            animation.setEndValue(1)
            animation.start()

        animation.finished.connect(on_fade_out)
        animation.start()

if __name__=="__main__":
    app=QApplication(sys.argv)
    QFontDatabase.addApplicationFont("fonts/MontserratAlternates-Medium.ttf")
    w=MusicPlayer()
    sys.exit(app.exec())