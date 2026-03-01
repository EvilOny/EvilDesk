import sys
import argparse
import time
import json
import asyncio
import threading

import random
from PIL.ImageQt import ImageQt
from PySide6.QtGui import QRadialGradient
from PIL import ImageFilter
from io import BytesIO

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QGraphicsBlurEffect, QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, QObject, Signal, QPropertyAnimation, QEasingCurve, Property, QTimer
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
import requests


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

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_player_state)
        self.poll_timer.start(1000)

        # ===== background =====
        self.bg_blur_label = QLabel(self.centralWidget())
        self.bg_blur_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_blur_label.lower()
        self.bg_blur_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.bg_gradient_label = QLabel(self.centralWidget())
        self.bg_gradient_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_gradient_label.lower()
        self.bg_gradient_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.bg_vignette_label = QLabel(self.centralWidget())
        self.bg_vignette_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_vignette_label.lower()
        self.bg_vignette_label.setAttribute(Qt.WA_TransparentForMouseEvents)

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

        self.likeBtn = self.ui.findChild(QPushButton, "likeBtn")
        self.volUpBtn = self.ui.findChild(QPushButton, "volUpBtn")
        self.volDownBtn = self.ui.findChild(QPushButton, "volDownBtn")
        self.prevBtn = self.ui.findChild(QPushButton, "prevBtn")
        self.playBtn = self.ui.findChild(QPushButton, "playBtn")
        self.nextBtn = self.ui.findChild(QPushButton, "nextBtn")

        # connection status
        self.status_label = QLabel(self.centralWidget())
        self.status_label.setText("Waiting for DockSync...")
        self.status_label.setStyleSheet("color: rgba(255,255,255,200); font-size:12px;")
        self.status_label.setGeometry(self.width()-200, 8, 190, 20)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.raise_()

        # ===== icons =====
        self.icons = {
            "vol_up": QIcon("icons/plus.png"),
            "vol_down": QIcon("icons/minus.png"),
            "like": QIcon("icons/like.png"),
            "prev": QIcon("icons/prev.png"),
            "play": QIcon("icons/play.png"),
            "pause": QIcon("icons/pause.png"),
            "next": QIcon("icons/next.png"),
        }
        self.volUpBtn.setIcon(self.icons["vol_up"])
        self.volDownBtn.setIcon(self.icons["vol_down"])
        self.likeBtn.setIcon(self.icons["like"])
        self.prevBtn.setIcon(self.icons["prev"])
        self.playBtn.setIcon(self.icons["play"])
        self.nextBtn.setIcon(self.icons["next"])
        for btn in (self.volUpBtn, self.volDownBtn, self.likeBtn, self.prevBtn, self.playBtn, self.nextBtn):
            btn.setIconSize(btn.size())

        # ===== exit =====
        self._touch_start_time = None
        self._touch_start_pos = None
        self._exit_timer = QTimer(self)
        self._exit_timer.setSingleShot(True)
        self._exit_timer.timeout.connect(self.safe_exit)

        # ===== button handlers =====
        self.prevBtn.clicked.connect(lambda: self.send_command({"request": "track", "message": -1}))
        self.playBtn.clicked.connect(lambda: self.send_command({"request": "playerInteraction"}))
        self.nextBtn.clicked.connect(lambda: self.send_command({"request": "track", "message": 1}))
        self.likeBtn.clicked.connect(lambda: self.send_command({"request": "likeInteraction"}))
        self.volUpBtn.clicked.connect(lambda: self.send_command({"request": "volume", "message": 0.05, "how": 1}))
        self.volDownBtn.clicked.connect(lambda: self.send_command({"request": "volume", "message": 0.05, "how": -1}))

        # ===== cover fade animation =====
        self.current_cover = ""
        self.cover_effect = QGraphicsOpacityEffect()
        self.cover.setGraphicsEffect(self.cover_effect)
        self.fade_anim = QPropertyAnimation(self.cover_effect, b"opacity")
        self.fade_anim.setDuration(350)
        self.fade_anim.setEasingCurve(QEasingCurve.InOutQuad)

        # ===== connect signals =====
        bridge.state_received.connect(self.update_from_ws)
        bridge.connected.connect(self.on_connected)
        bridge.disconnected.connect(self.on_disconnected)

        # ===== WebSocket server =====
        self._ws_loop = None
        self._ws_loop_thread = None
        self._clients = set()
        self.start_ws_thread()

        self.apply_styles()

    # ===== Property для анимации фона =====
    def get_bg_t(self):
        return self._bg_t

    def set_bg_t(self, val):
        self._bg_t = val

    bg_t = Property(float, get_bg_t, set_bg_t)

    # ===== WebSocket сервер =====
    def start_ws_thread(self):
        def thread_target():
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            self._ws_loop.run_until_complete(self.start_server(self.ws_host, self.ws_port))
        self._ws_loop_thread = threading.Thread(target=thread_target, daemon=True)
        self._ws_loop_thread.start()

    async def start_server(self, host="0.0.0.0", port=8765):
        self.ws_host = host
        self.ws_port = port

        async def ws_handler(websocket):
            self._clients.add(websocket)
            bridge.connected.emit()
            try:
                async for msg in websocket:
                    print("WS received:", msg)
                    try:
                        data = json.loads(msg)
                        print("Parsed data:", data)
                        bridge.state_received.emit(data)
                    except Exception as e:
                        print("JSON error:", e)
            except websockets.ConnectionClosed:
                pass
            finally:
                self._clients.remove(websocket)
                bridge.disconnected.emit()

        async with websockets.serve(ws_handler, host, port):
            print(f"WS Server started at {host}:{port}")
            await asyncio.Future()  # never stop

    def send_command(self, payload: dict):
        if self._ws_loop is None or not self._clients:
            return

        async def _send():
            for ws in list(self._clients):
                try:
                    await ws.send(json.dumps(payload))
                except Exception:
                    pass


        asyncio.run_coroutine_threadsafe(_send(), self._ws_loop)


    # ===== GUI события =====
    def on_connected(self):
        self.status_label.setText("DockSync connected")
        self.status_label.setStyleSheet("color: lightgreen; font-size:12px;")

    def on_disconnected(self):
        self.status_label.setText("DockSync disconnected")
        self.status_label.setStyleSheet("color: salmon; font-size:12px;")

    def poll_player_state(self):
        self.send_command({"request": "coverImage"})
        self.send_command({"request": "playingState"})
        self.send_command({"request": "likeState"})
        self.send_command({"request": "trackName"})
        self.send_command({"request": "artistName"})

    def update_from_ws(self, data: dict):
        try:
            req = data.get("request")
            resp = data.get("response")

            if req == "playingState":
                if resp == 0:
                    self.playBtn.setIcon(self.icons["pause"])
                else:
                    self.playBtn.setIcon(self.icons["play"])

            elif req == "likeState":
                if resp == 1:
                    self.likeBtn.setIcon(QIcon("icons/like_active.png"))
                else:
                    self.likeBtn.setIcon(self.icons["like"])

            elif req == "coverImage":
                if isinstance(resp, str) and resp.startswith("http"):
                    if self.current_cover != resp:
                        self.load_cover_from_url(resp)
                        self.current_cover = resp
                        data = requests.get(resp).content
                        self.set_background_from_bytes(data)

            elif req == "trackName":
                self.track_title.setText(resp or "—")

            elif req == "artistName":
                self.artist.setText(resp or "—")

        except Exception as e:
            print("Error updating GUI:", e)


    def load_cover_from_url(self, url: str):
        """
        Загружает обложку трека по URL и анимирует её появление на экране.
        """
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            img_bytes = resp.content

            qpix = QPixmap()
            if not qpix.loadFromData(img_bytes):
                print("Failed to load cover from data")
                return

            pix = qpix.scaled(self.cover.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)

            self.fade_anim.stop()
            self.cover_effect.setOpacity(0.0)
            self.cover.setPixmap(pix)
            self.fade_anim.setStartValue(0.0)
            self.fade_anim.setEndValue(1.0)
            self.fade_anim.start()

            self.animate_bg_from_bytes(img_bytes)

        except Exception as e:
            print("Error loading cover:", e)

    # ===== Остальной код GUI и анимации =====
    def interpolate_color(self, c1, c2, t):
        r = int(c1[0] + (c2[0]-c1[0])*t)
        g = int(c1[1] + (c2[1]-c1[1])*t)
        b = int(c1[2] + (c2[2]-c1[2])*t)
        return r, g, b

    def set_background_from_bytes(self, img_bytes: bytes):
        """
        Устанавливает полный стек фона: блюр, градиент, виньетка, шум.
        """
        try:
            # --- 1. Создаём размытый pixmap ---
            img = Image.open(BytesIO(img_bytes)).convert("RGB")
            img = img.resize((self.width() * 2, self.height() * 2), Image.LANCZOS)  # overscale для мягкости
            img = img.filter(ImageFilter.GaussianBlur(40))  # сильный блюр

            qt_img = ImageQt(img)
            pix = QPixmap.fromImage(qt_img)
            self.bg_blur_label.setPixmap(pix)
            self.bg_blur_label.setGeometry(-self.width() // 2, -self.height() // 2,
                                           self.width() * 2, self.height() * 2)

            # --- 2. Цветовой градиент ---
            r, g, b = self.extract_main_color(img)
            grad_pix = QPixmap(self.size())
            grad_pix.fill(Qt.transparent)

            painter = QPainter(grad_pix)
            linear_grad = QLinearGradient(0, 0, 0, self.height())
            linear_grad.setColorAt(0.0, QColor(r, g, b, 180))
            linear_grad.setColorAt(1.0, QColor(r // 3, g // 3, b // 3, 150))
            painter.fillRect(grad_pix.rect(), linear_grad)
            painter.end()
            self.bg_gradient_label.setPixmap(grad_pix)

            # --- 3. Виньетка ---
            vignette_pix = QPixmap(self.size())
            vignette_pix.fill(Qt.transparent)
            painter = QPainter(vignette_pix)
            radial_grad = QRadialGradient(
                self.width() // 2,
                self.height() // 2,
                max(self.width(), self.height()) * 0.7
            )
            radial_grad.setColorAt(0.6, QColor(0, 0, 0, 0))
            radial_grad.setColorAt(1.0, QColor(0, 0, 0, 180))
            painter.fillRect(vignette_pix.rect(), radial_grad)
            painter.end()
            self.bg_vignette_label.setPixmap(vignette_pix)

            # --- 4. Лёгкий шум (убирает ступенчатость) ---
            self.add_noise_to_label(self.bg_vignette_label)

        except Exception as e:
            print("Ошибка обновления фона:", e)

    # ------------------------------
    # 2. Извлечение основного цвета через ColorThief или PIL
    # ------------------------------
    def extract_main_color(self, img: Image.Image):
        """
        Возвращает основной цвет картинки. Если белый фон с чёрным силуэтом,
        приоритет отдаём более светлому цвету.
        """
        try:
            pixels = list(img.getdata())
            # создаём словарь {цвет: кол-во}
            count = {}
            for px in pixels:
                count[px] = count.get(px, 0) + 1

            # сортируем по яркости (luminance) с приоритетом светлого цвета
            def lum(c):
                return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

            # берём 10 самых частых цветов
            top_colors = sorted(count.items(), key=lambda x: x[1], reverse=True)[:10]
            dominant = max([c for c, _ in top_colors], key=lum)
            return dominant
        except Exception as e:
            print("extract_main_color error:", e)
            return (50, 50, 50)  # дефолт серый

    # ------------------------------
    # 3. Лёгкий шум
    # ------------------------------
    def add_noise_to_label(self, label: QLabel):
        pix = label.pixmap()
        if pix is None:
            return
        painter = QPainter(pix)
        painter.setCompositionMode(QPainter.CompositionMode_Overlay)
        for _ in range(5000):
            x = random.randint(0, pix.width() - 1)
            y = random.randint(0, pix.height() - 1)
            alpha = random.randint(5, 15)
            painter.fillRect(x, y, 1, 1, QColor(255, 255, 255, alpha))
        painter.end()
        label.setPixmap(pix)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            if pos.x() < 80 and pos.y() > self.height() - 80:
                self._touch_start_time = time.time()
                self._touch_start_pos = pos
                self._exit_timer.start(3500)

    def mouseReleaseEvent(self, event):
        self._exit_timer.stop()
        self._touch_start_time = None
        self._touch_start_pos = None

    def safe_exit(self):
        try:
            if self._ws_loop:
                self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        except Exception:
            pass
        QApplication.quit()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, "bg_label") and self.bg_label:
            self.bg_label.setGeometry(0, 0, self.width(), self.height())
        if hasattr(self, "status_label"):
            self.status_label.setGeometry(self.width() - 200, 8, 190, 20)
        if hasattr(self, "cover") and self.cover and self.cover.pixmap():
            self.cover.setPixmap(
                self.cover.pixmap().scaled(
                    self.cover.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            )

    def apply_styles(self):
        self.setStyleSheet("""
        QMainWindow { background: #141414; }

        #trackTitleLabel, #artistLabel {
            color: white;
            font-size: 18px;
            font-family: "Montserrat Alternates", "Segoe UI", "Arial Rounded MT", "Roboto", sans-serif;
            font-weight: 500;
        }

        QFrame#controlPanel { 
            background: rgba(255,255,255,20); 
            border-radius: 18px; 
        }

        QPushButton {
            background: transparent;
            border: none;
            padding: 8px;
            border-radius: 14px;
        }
        """)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws_host", type=str, default="0.0.0.0",
                        help="WebSocket server host, e.g. 192.168.1.100")
    parser.add_argument("--ws_port", type=int, default=8765,
                        help="WebSocket server port")
    args = parser.parse_args()

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
    w.ws_host = args.ws_host
    w.ws_port = args.ws_port

    sys.exit(app.exec())