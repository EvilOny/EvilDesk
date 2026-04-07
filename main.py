import sys
import json
import asyncio
import logging
import threading
from concurrent.futures import CancelledError
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
from websockets.exceptions import ConnectionClosed


# ================= BRIDGE =================

class WSBridge(QObject):
    state_received = Signal(dict)
    cover_loaded = Signal(str, bytes)


bridge = WSBridge()
logger = logging.getLogger(__name__)


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
        #self.showFullScreen()
        #self.setCursor(Qt.BlankCursor)

        self._clients = set()
        self._ws_loop = None
        self._ws_server = None
        self._ws_thread = None
        self._ws_shutdown_future = None
        self._is_shutting_down = False

        self.current_cover = ""
        self.current_bg_colors = None
        self.old_bg_colors = None
        self.new_bg_colors = None
        self._bg_t = 0.0

        self.is_playing = False
        self.is_liked = False

        self._exit_timer = QTimer(self)
        self._exit_timer.setSingleShot(True)
        self._exit_timer.timeout.connect(self.safe_exit)

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
        self._ws_thread = threading.Thread(target=self.start_ws, daemon=True)
        self._ws_thread.start()

    def start_ws(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        self._ws_shutdown_future = loop.create_future()

        try:
            self._ws_server = loop.run_until_complete(self.start_server())
            loop.run_until_complete(self._ws_shutdown_future)
        except Exception:
            logger.exception("WebSocket loop crashed")
        finally:
            loop.run_until_complete(self.shutdown_ws_server())
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            self._ws_server = None
            self._ws_shutdown_future = None
            self._ws_loop = None

    async def start_server(self):
        async def handler(ws):
            self._clients.add(ws)
            try:
                async for msg in ws:
                    try:
                        bridge.state_received.emit(json.loads(msg))
                    except json.JSONDecodeError:
                        logger.exception("Failed to decode WebSocket message: %s", msg)
            finally:
                self._clients.discard(ws)

        server = await websockets.serve(handler, "0.0.0.0", 8765)
        logger.info("WebSocket server started on 0.0.0.0:8765")
        return server

    async def shutdown_ws_server(self):
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                logger.exception("Failed to close WebSocket client cleanly")
        self._clients.clear()

        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            logger.info("WebSocket server stopped")

    def request_ws_shutdown(self):
        if not self._ws_loop or self._ws_loop.is_closed():
            return

        def stop_loop():
            if self._ws_shutdown_future and not self._ws_shutdown_future.done():
                self._ws_shutdown_future.set_result(None)

        self._ws_loop.call_soon_threadsafe(stop_loop)

    def _log_send_result(self, future):
        try:
            future.result()
        except CancelledError:
            logger.debug("WebSocket send was cancelled")
        except Exception:
            logger.exception("WebSocket send failed")

    def send_command(self, payload):
        if not self._ws_loop or self._ws_loop.is_closed() or not self._clients:
            return

        message = json.dumps(payload)

        async def send():
            for ws in list(self._clients):
                try:
                    await ws.send(message)
                except ConnectionClosed:
                    self._clients.discard(ws)
                    logger.warning("Dropped disconnected WebSocket client")
                except Exception:
                    self._clients.discard(ws)
                    logger.exception("Failed to send command: %s", payload)

        future = asyncio.run_coroutine_threadsafe(send(), self._ws_loop)
        future.add_done_callback(self._log_send_result)

    # ================= UPDATE =================

    def load_cover(self, url):
        try:
            r = requests.get(url, timeout=2)
            r.raise_for_status()
            bridge.cover_loaded.emit(url, r.content)
        except Exception:
            logger.exception("Failed to load cover from %s", url)

    def apply_cover(self, url, img_bytes):
        if url != self.current_cover:
            return

        qpix = QPixmap()
        if not qpix.loadFromData(img_bytes):
            logger.error("Failed to decode cover image from %s", url)
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

            animation.stop()
            animation.setStartValue(0)
            animation.setEndValue(1)
            animation.start()

        animation.finished.connect(on_fade_out)
        animation.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            if pos.x() < 80 and pos.y() > self.height() - 80:
                self._exit_timer.start(3500)

    def mouseReleaseEvent(self, event):
        if self._exit_timer.isActive():
            self._exit_timer.stop()
        super().mouseReleaseEvent(event)

    def cleanup(self):
        if self._is_shutting_down:
            return

        self._is_shutting_down = True
        self._exit_timer.stop()
        if hasattr(self, "poll_timer"):
            self.poll_timer.stop()
        self.request_ws_shutdown()

        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2)

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def safe_exit(self):
        self.cleanup()
        QApplication.quit()

if __name__=="__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app=QApplication(sys.argv)
    QFontDatabase.addApplicationFont("fonts/MontserratAlternates-Medium.ttf")
    w=MusicPlayer()
    sys.exit(app.exec())
