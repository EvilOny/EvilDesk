#!/usr/bin/env python3
"""
EvilDesk Player — PySide6 Desktop Application
Управление Яндекс Музыкой через WebSocket-плагин PulseSync.

Функции:
- Кнопки управления воспроизведением
- Регулировка громкости
- Прогресс-бар трека
- Динамическая тема на основе обложки
- Плавные анимации переходов

Автор: Andrei Filippov
Лицензия: MIT
"""

import sys
import json
import asyncio
import logging
import threading
import argparse
from io import BytesIO
from datetime import datetime
from typing import Optional, Tuple, List

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton,
    QGraphicsBlurEffect, QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect, QFrame, QProgressBar,
    QVBoxLayout, QHBoxLayout, QWidget, QGraphicsView,
    QGraphicsScene, QGraphicsScale, QSizePolicy
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QPropertyAnimation, QTimer,
    QParallelAnimationGroup, QSequentialAnimationGroup,
    QEasingCurve, Property, QSize, QPoint, Slot, QPointF
)
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QLinearGradient, QRadialGradient,
    QBrush, QPainter, QFontDatabase, QMouseEvent, QFont, QVector3D
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import QFile

from PIL import Image

try:
    from colorthief import ColorThief

    _HAS_COLORTHIEF = True
except ImportError:
    _HAS_COLORTHIEF = False
    logging.warning("⚠️ colorthief не установлен — используется упрощённое извлечение цвета")

import websockets
import requests
from websockets.exceptions import ConnectionClosed, InvalidState

# ======================== КОНФИГУРАЦИЯ ========================
CONFIG = {
    "ws_host": "0.0.0.0",
    "ws_port": 8765,
    "poll_interval_ms": 2000,
    "animation_duration_ms": 300,
    "cover_size": (280, 280),
    "log_level": logging.INFO,
}


# ======================== ЛОГИРОВАНИЕ ========================
def setup_logging(level: int = logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Раскомментируйте для логирования в файл:
            # logging.FileHandler("evildesk.log", encoding="utf-8", mode="a")
        ]
    )


logger = logging.getLogger(__name__)


# ======================== SIGNAL BRIDGE ========================
class SignalBridge(QObject):
    """Мост между asyncio и Qt-потоками"""
    state_received = Signal(dict)
    cover_loaded = Signal(str, bytes)
    connection_changed = Signal(bool)
    progress_updated = Signal(float)  # 0.0 - 1.0


bridge = SignalBridge()


# ======================== АНИМАЦИИ ========================
class FadeAnimation:
    """Утилита для плавного изменения прозрачности"""

    def __init__(self, target, property_name: bytes, duration: int = 300):
        self.effect = QGraphicsOpacityEffect(target)
        target.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, property_name)
        self.anim.setDuration(duration)
        self.anim.setEasingCurve(QEasingCurve.InOutQuad)

    def fade(self, start: float, end: float, on_finished=None):
        self.anim.setStartValue(start)
        self.anim.setEndValue(end)
        if on_finished:
            # Безопасное подключение сигнала
            try:
                self.anim.finished.disconnect()
            except:
                pass
            self.anim.finished.connect(on_finished)
        self.anim.start()

    def set_opacity(self, value: float):
        self.effect.setOpacity(value)


class TextFade:
    """Плавная смена текста с эффектом затухания"""

    def __init__(self, label, duration: int = 300):
        self.label = label
        self.effect = QGraphicsOpacityEffect(label)
        label.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity")
        self.anim.setDuration(duration)
        self.anim.setEasingCurve(QEasingCurve.InOutQuad)

    def change(self, new_text: str):
        if self.label.text() == new_text:
            return

        # Анимация: 1.0 → 0.0 → смена текста → 0.0 → 1.0
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)

        def on_fade_out():
            try:
                self.anim.finished.disconnect(on_fade_out)
            except:
                pass
            self.label.setText(new_text)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.start()

        self.anim.finished.connect(on_fade_out)
        self.anim.start()


# ======================== ЦВЕТОВАЯ ТЕМА ========================
class ThemeEngine:
    """Извлечение и анимация цветовой темы из обложки"""

    @staticmethod
    def extract_dominant_colors(img_bytes: bytes) -> List[Tuple[int, int, int]]:
        """Извлекает 2 доминирующих цвета для градиента"""
        try:
            if _HAS_COLORTHIEF:
                ct = ColorThief(BytesIO(img_bytes))
                palette = ct.get_palette(color_count=2, quality=1)
                return [tuple(c) for c in palette[:2]]
            else:
                # Упрощённый метод: средний цвет + затемнённый
                img = Image.open(BytesIO(img_bytes)).convert("RGB")
                img = img.resize((50, 50), Image.Resampling.LANCZOS)
                pixels = list(img.getdata())
                avg = tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))
                dark = tuple(max(0, c // 3) for c in avg)
                return [avg, dark]
        except Exception as e:
            logger.warning(f"⚠️ Ошибка извлечения цветов: {e}")
            return [(30, 30, 40), (10, 10, 15)]  # Fallback

    @staticmethod
    def create_gradient_pixmap(size: QSize, colors: List[Tuple[int, int, int]]) -> QPixmap:
        """Создаёт градиентный фон"""
        pixmap = QPixmap(size)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        grad = QLinearGradient(0, 0, 0, size.height())

        # Верхний цвет с прозрачностью
        c1 = QColor(*colors[0])
        c1.setAlpha(230)
        grad.setColorAt(0.3, c1)

        # Нижний цвет более тёмный
        c2 = QColor(*colors[1])
        c2.setAlpha(200)
        grad.setColorAt(1.0, c2)

        painter.fillRect(pixmap.rect(), QBrush(grad))
        painter.end()
        return pixmap

    @staticmethod
    def interpolate_color(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
        """Интерполяция между двумя цветами"""
        t = max(0.0, min(1.0, t))
        return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


# ======================== ПРОГРЕСС-БАР ТРЕКА ========================
class TrackProgress:
    """Виртуальный прогресс-бар (эмуляция, т.к. плагин не отдаёт время)"""

    def __init__(self, parent, update_callback=None):
        self.progress_bar = QProgressBar(parent)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: rgba(255,255,255,0.1);
                border-radius: 2px;
                border: none;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7c3aed, stop:1 #4ade80);
                border-radius: 2px;
            }
        """)

        self.timer = QTimer(parent)
        self.timer.setInterval(1000)  # Обновление каждую секунду
        self.timer.timeout.connect(self._tick)

        self.is_playing = False
        self.current_value = 0
        self.update_callback = update_callback

        # Скрываем по умолчанию (пока нет данных о треке)
        self.progress_bar.hide()

    def show(self):
        self.progress_bar.show()
        if self.is_playing:
            self.timer.start()

    def hide(self):
        self.progress_bar.hide()
        self.timer.stop()

    def set_playing(self, playing: bool):
        self.is_playing = playing
        if playing:
            self.timer.start()
        else:
            self.timer.stop()

    def reset(self):
        self.current_value = 0
        self.progress_bar.setValue(0)

    def _tick(self):
        """Эмуляция прогресса (3 минуты = 180 секунд = 100%)"""
        if self.is_playing:
            self.current_value = min(100, self.current_value + 100 / 180)
            self.progress_bar.setValue(int(self.current_value))
            if self.update_callback:
                self.update_callback(self.current_value / 100)


# ======================== MAIN WINDOW ========================
class MusicPlayer(QMainWindow):

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        super().__init__()

        # Настройки
        self.ws_host = host
        self.ws_port = port

        # WebSocket состояние
        self._clients = set()
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_server = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_shutdown_future = None
        self._is_shutting_down = False

        # UI компоненты
        self.cover_label: Optional[QLabel] = None
        self.track_title: Optional[QLabel] = None
        self.artist_label: Optional[QLabel] = None
        self.play_btn: Optional[QPushButton] = None
        self.like_btn: Optional[QPushButton] = None
        self.prev_btn: Optional[QPushButton] = None
        self.next_btn: Optional[QPushButton] = None
        self.vol_up_btn: Optional[QPushButton] = None
        self.vol_down_btn: Optional[QPushButton] = None
        self.control_panel: Optional[QFrame] = None
        self.progress: Optional[TrackProgress] = None

        # Анимации
        self.cover_fade: Optional[FadeAnimation] = None
        self.title_fade: Optional[TextFade] = None
        self.artist_fade: Optional[TextFade] = None
        #self.bg_anim: Optional[QPropertyAnimation] = None

        # Данные
        self.current_cover_url: Optional[str] = None
        self.cover_cache: dict = {}  # url -> (img_bytes, colors)
        self.current_bg_colors: Optional[List[Tuple[int, int, int]]] = None
        self._bg_t = 0.0  # Для анимации фона

        # Состояние плеера
        self.is_playing = False
        self.is_liked = False

        # Иконки
        self.icons = {}

        # Таймеры
        self.poll_timer = QTimer(self)
        self.exit_timer = QTimer(self)
        self.exit_timer.setSingleShot(True)
        self.exit_timer.timeout.connect(self.safe_exit)

        # Инициализация
        self._setup_logging()
        self._init_ui()
        self._init_animations()
        self._init_background()
        self._init_progress()
        self._init_icons()
        self._connect_signals()
        self._apply_styles()
        self._create_edge_shadow()
        self._start_websocket_server()
        self._start_polling()

        logger.info("🚀 EvilDesk Player запущен")
        self.show()

    # ======================== ЛОГИРОВАНИЕ ========================
    def _setup_logging(self):
        setup_logging(CONFIG["log_level"])

    # ======================== UI ========================
    def _init_ui(self):
        """Инициализация интерфейса из .ui файла"""
        loader = QUiLoader()
        ui_file = QFile("main.ui")
        if not ui_file.open(QFile.ReadOnly):
            logger.error("❌ Не удалось открыть main.ui")
            # Создаём минимальный UI программно
            self._create_fallback_ui()
            return

        self.ui = loader.load(ui_file)
        ui_file.close()

        if not self.ui:
            logger.error("❌ Не удалось загрузить UI из main.ui")
            self._create_fallback_ui()
            return

        self.setCentralWidget(self.ui)
        self.resize(800, 520)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        # Поиск элементов
        self.cover_label = self.ui.findChild(QLabel, "coverLabel")
        self.track_title = self.ui.findChild(QLabel, "trackTitleLabel")
        self.artist_label = self.ui.findChild(QLabel, "artistLabel")
        self.control_panel = self.ui.findChild(QFrame, "controlPanel")

        if self.control_panel and self.control_panel.layout():
            self.control_panel.layout().setContentsMargins(16, 14, 16, 14)
            self.control_panel.layout().setSpacing(12)

        # Кнопки
        self.play_btn = self.ui.findChild(QPushButton, "playBtn")
        self.like_btn = self.ui.findChild(QPushButton, "likeBtn")
        self.prev_btn = self.ui.findChild(QPushButton, "prevBtn")
        self.next_btn = self.ui.findChild(QPushButton, "nextBtn")
        self.vol_up_btn = self.ui.findChild(QPushButton, "volUpBtn")
        self.vol_down_btn = self.ui.findChild(QPushButton, "volDownBtn")

        # === НАСТРОЙКА КНОПОК (Liquid Glass) ===
        btn_size = QSize(78, 78)
        icon_size = QSize(42, 42)

        for btn in [self.play_btn, self.like_btn, self.prev_btn, self.next_btn,
                    self.vol_up_btn, self.vol_down_btn]:
            if btn:
                btn.setCursor(Qt.PointingHandCursor)
                btn.setIconSize(icon_size)
                btn.setFixedSize(btn_size)
                # ✅ Убрано setTransformations() — он не работает с QWidget
                btn.setStyleSheet("""
                    QPushButton {
                        background: transparent;
                        border: 1px solid rgba(255, 255, 255, 0.0);
                        border-radius: 39px;
                        padding: 4px;
                    }
                    QPushButton:hover {
                        background: rgba(255, 255, 255, 0.14);
                        border: 1px solid rgba(255, 255, 255, 0.25);
                    }
                    QPushButton:pressed {
                        background: rgba(255, 255, 255, 0.08);
                        padding: 7px 3px 3px 7px; /* Сдвиг имитирует нажатие */
                        border: 1px solid rgba(255, 255, 255, 0.1);
                    }
                """)

        if self.control_panel and self.control_panel.layout():
            layout = self.control_panel.layout()
            layout.setContentsMargins(18, 14, 18, 14)
            layout.setSpacing(14)

            # Явное выравнивание каждого элемента по центру
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item.widget():
                    layout.setAlignment(item.widget(), Qt.AlignVCenter | Qt.AlignHCenter)

                # === ГАРАНТИРОВАННОЕ ЦЕНТРИРОВАНИЕ ПО ВЕРТИКАЛИ ===
                layout = self.control_panel.layout()
                if layout:
                    # 1. Запрещаем кнопкам растягиваться по вертикали
                    fixed_policy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

                    # 2. Сбрасываем выравнивание лейаута
                    layout.setAlignment(Qt.AlignVCenter)
                    layout.setContentsMargins(16, 6, 16, 6)  # Симметричные отступы
                    layout.setSpacing(14)

                    # 3. Применяем политику и явное выравнивание к каждой кнопке
                    for btn in [self.play_btn, self.like_btn, self.prev_btn, self.next_btn,
                                self.vol_up_btn, self.vol_down_btn]:
                        if btn:
                            btn.setSizePolicy(fixed_policy)
                            layout.setAlignment(btn, Qt.AlignVCenter | Qt.AlignHCenter)

    def _create_fallback_ui(self):
        """Создаёт минимальный UI если .ui файл не загружен"""
        logger.warning("⚠️ Используем fallback UI")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Обложка
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(280, 280)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setStyleSheet("background: #2a2a2a; border-radius: 16px;")
        self.cover_label.setText("🎵")
        layout.addWidget(self.cover_label, alignment=Qt.AlignCenter)

        # Текст
        self.track_title = QLabel("Название трека")
        self.track_title.setAlignment(Qt.AlignCenter)
        self.track_title.setStyleSheet("color: white; font-size: 20px; font-weight: bold;")
        layout.addWidget(self.track_title)

        self.artist_label = QLabel("Исполнитель")
        self.artist_label.setAlignment(Qt.AlignCenter)
        self.artist_label.setStyleSheet("color: #aaa; font-size: 14px;")
        layout.addWidget(self.artist_label)

        # Кнопки
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        for text, name in [("⏮️", "prev"), ("▶️", "play"), ("⏭️", "next"), ("❤️", "like")]:
            btn = QPushButton(text)
            btn.setFixedSize(50, 50)
            btn.setProperty("name", name)
            btn.setStyleSheet("""
                QPushButton {
                    background: rgba(255,255,255,0.1);
                    border: none;
                    border-radius: 25px;
                    font-size: 20px;
                    color: white;
                }
                QPushButton:hover { background: rgba(255,255,255,0.2); }
            """)
            btn_layout.addWidget(btn)

        layout.addLayout(btn_layout)

        self.setCentralWidget(central)
        self.resize(400, 500)
        self.setWindowTitle("EvilDesk Player")

    def _init_animations(self):
        """Инициализация анимаций"""
        dur = CONFIG["animation_duration_ms"]

        self.cover_fade = FadeAnimation(self.cover_label, b"opacity", duration=dur)
        self.title_fade = TextFade(self.track_title, duration=dur)
        self.artist_fade = TextFade(self.artist_label, duration=dur)

        # Анимация фона
        #self.bg_anim = QPropertyAnimation(self, b"bg_t")
        #self.bg_anim.setDuration(CONFIG["bg_animation_duration_ms"])
        #self.bg_anim.setEasingCurve(QEasingCurve.InOutQuad)
        #self.bg_anim.valueChanged.connect(self._update_bg_gradient)

    def _init_background(self):
        """Инициализация фона: размытая обложка вместо градиента"""
        self.bg_label = QLabel(self.centralWidget())
        self.bg_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_label.lower()
        self.bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # Клики проходят сквозь фон
        self.bg_label.setStyleSheet("background: #0d0d0d;")  # Цвет-заглушка, если обложки нет

        # Нативное размытие Qt (работает быстро и без блокировки UI)
        self.bg_blur = QGraphicsBlurEffect()
        self.bg_blur.setBlurRadius(70)  # Настройте под себя: 40-100
        self.bg_label.setGraphicsEffect(self.bg_blur)

        self._current_cover_bytes = None  # Для перерисовки при ресайзе окна

    def _init_progress(self):
        """Инициализация прогресс-бара"""
        self.progress = TrackProgress(
            self.centralWidget(),
            update_callback=self._on_progress_update
        )
        self.http_session = requests.Session()
        self.http_session.headers.update({
            "User-Agent": "EvilDesk/1.0",
            "Accept": "image/*,*/*;q=0.8",
            "Connection": "keep-alive"
        })
        if self.control_panel and self.control_panel.layout():
            self.control_panel.layout().insertWidget(0, self.progress.progress_bar)

    def _init_icons(self):
        """Загрузка иконок"""
        icon_map = {
            "play": "icons/play.png",
            "pause": "icons/pause.png",
            "next": "icons/next.png",
            "prev": "icons/prev.png",
            "like": "icons/like.png",
            "like_active": "icons/like_active.png",
            "vol_up": "icons/plus.png",
            "vol_down": "icons/minus.png",
        }

        for name, path in icon_map.items():
            try:
                self.icons[name] = QIcon(path)
            except:
                # Fallback на текстовые иконки
                self.icons[name] = QIcon()

        # Установка иконок
        if self.play_btn:
            self.play_btn.setIcon(self.icons.get("play", QIcon()))
        if self.like_btn:
            self.like_btn.setIcon(self.icons.get("like", QIcon()))
        if self.prev_btn:
            self.prev_btn.setIcon(self.icons.get("prev", QIcon()))
        if self.next_btn:
            self.next_btn.setIcon(self.icons.get("next", QIcon()))
        if self.vol_up_btn:
            self.vol_up_btn.setIcon(self.icons.get("vol_up", QIcon()))
        if self.vol_down_btn:
            self.vol_down_btn.setIcon(self.icons.get("vol_down", QIcon()))

    def _connect_signals(self):
        """Подключение сигналов к слотам"""
        # Кнопки управления
        if self.prev_btn:
            self.prev_btn.clicked.connect(lambda: self._cmd_track(-1))
        if self.next_btn:
            self.next_btn.clicked.connect(lambda: self._cmd_track(1))
        if self.play_btn:
            self.play_btn.clicked.connect(self._cmd_play_pause)
        if self.like_btn:
            self.like_btn.clicked.connect(self._cmd_like)
        if self.vol_up_btn:
            self.vol_up_btn.clicked.connect(
                lambda: self._send_command({"request": "volume", "message": 0.05, "how": 1})
            )
        if self.vol_down_btn:
            self.vol_down_btn.clicked.connect(
                lambda: self._send_command({"request": "volume", "message": 0.05, "how": -1})
            )

        # Сигналы от bridge
        bridge.state_received.connect(self._handle_ws_message)
        bridge.cover_loaded.connect(self._apply_cover_image)
        bridge.connection_changed.connect(self._update_connection_indicator)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background: #080808;
            }

            /* === ТЕКСТ === */
            QLabel#trackTitleLabel {
                color: rgba(255, 255, 255, 0.92);
                font-size: 25px;
                font-weight: 600;
                font-family: "SF Pro Display", "Inter", "Segoe UI", system-ui, sans-serif;
                qproperty-alignment: 'AlignCenter';
                letter-spacing: -0.5px;
            }
            QLabel#artistLabel {
                color: rgba(255, 255, 255, 0.55);
                font-size: 16px;
                font-weight: 400;
                font-family: "SF Pro Display", "Inter", "Segoe UI", system-ui, sans-serif;
                qproperty-alignment: 'AlignCenter';
                margin-top: -2px;
            }

            /* === LIQUID GLASS PANEL === */
            QFrame#controlPanel {
                /* Полупрозрачный стеклянный фон с верхним бликом */
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 rgba(255, 255, 255, 0.12), 
                    stop:0.4 rgba(255, 255, 255, 0.04),
                    stop:1 rgba(255, 255, 255, 0.01));
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-top: 1px solid rgba(255, 255, 255, 0.28); /* Верхний блик */
                border-radius: 30px;
                margin: 14px 28px 10px 28px;
            }

            /* === ПРОГРЕСС-БАР === */
            QProgressBar {
                background: rgba(255, 255, 255, 0.10);
                border-radius: 4px;
                border: none;
                margin: 0 14px;
                height: 6px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(124, 58, 237, 0.95), 
                    stop:0.5 rgba(99, 102, 241, 0.95),
                    stop:1 rgba(74, 222, 128, 0.95));
                border-radius: 4px;
                box-shadow: 0 0 8px rgba(124, 58, 237, 0.4);
            }

            /* === КНОПКИ (базовые) === */
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 39px;
                padding: 0;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.14);
            }
            QPushButton:pressed {
                background: rgba(255, 255, 255, 0.06);
            }
        """)

    def _create_edge_shadow(self):
        """Создание виньетки по краям"""
        self.edge_overlay = QLabel(self)
        self.edge_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.edge_overlay.raise_()
        self._update_edge_shadow()

    def _update_edge_shadow(self):
        """Обновление виньетки при изменении размера"""
        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)

        painter = QPainter(pix)
        grad = QRadialGradient(
            self.width() / 2, self.height() / 2,
            max(self.width(), self.height()) / 1.15
        )
        grad.setColorAt(0.65, QColor(0, 0, 0, 0))
        grad.setColorAt(0.85, QColor(0, 0, 0, 80))
        grad.setColorAt(1.0, QColor(0, 0, 0, 200))

        painter.fillRect(pix.rect(), QBrush(grad))
        painter.end()

        self.edge_overlay.setPixmap(pix)
        self.edge_overlay.setGeometry(0, 0, self.width(), self.height())

    # ======================== WEBSOCKET SERVER ========================
    def _start_websocket_server(self):
        """Запуск WebSocket-сервера с надёжной привязкой к порту"""

        def run_async_loop():
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            self._ws_shutdown_future = self._ws_loop.create_future()

            async def serve():
                # ✅ reuse_address=True разрешает быстрый перезапуск без TIME_WAIT
                # ✅ ping_interval/timeout детектят "зависшие" соединения
                server = await websockets.serve(
                    self._ws_handler,
                    self.ws_host,
                    self.ws_port,
                    reuse_address=True,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=5
                )
                logger.info(f"🌐 Сервер готов: ws://{self.ws_host}:{self.ws_port}")
                await self._ws_shutdown_future

            try:
                self._ws_loop.run_until_complete(serve())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"❌ Ошибка сервера: {e}", exc_info=True)
            finally:
                self._ws_loop.run_until_complete(self._shutdown_ws())
                self._ws_loop.close()

        self._ws_thread = threading.Thread(target=run_async_loop, daemon=True)
        self._ws_thread.start()

    async def _ws_handler(self, websocket):
        """Обработчик подключений плагина"""
        self._clients.add(websocket)
        logger.info(f"🔗 Плагин подключился: {websocket.remote_address}")
        bridge.connection_changed.emit(True)

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    bridge.state_received.emit(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"⚠️ Ошибка парсинга JSON: {e}")
        except ConnectionClosed as e:
            logger.info(f"🔌 Соединение закрыто: {e}")
        except Exception as e:
            logger.error(f"❌ Ошибка в обработчике: {e}", exc_info=True)
        finally:
            self._clients.discard(websocket)
            if not self._clients:
                bridge.connection_changed.emit(False)
                logger.info("🧹 Все клиенты отключены")

    async def _shutdown_ws(self):
        """Корректное завершение WebSocket-сервера"""
        # Закрываем все соединения
        for ws in list(self._clients):
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось закрыть соединение: {e}")
        self._clients.clear()

        # Закрываем сервер
        if self._ws_server:
            self._ws_server.close()
            try:
                await asyncio.wait_for(self._ws_server.wait_closed(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("⚠️ Таймаут при закрытии сервера")

    def _request_ws_shutdown(self):
        """Запрос завершения WebSocket-сервера из главного потока"""
        if not self._ws_loop or self._ws_loop.is_closed():
            return
        if self._ws_shutdown_future and not self._ws_shutdown_future.done():
            self._ws_loop.call_soon_threadsafe(
                lambda: self._ws_shutdown_future.set_result(None)
            )

    # ======================== ОТПРАВКА КОМАНД ========================
    def _send_command(self, payload: dict) -> bool:
        """Отправка команды плагину"""
        if not self._ws_loop or self._ws_loop.is_closed() or not self._clients:
            logger.debug("⚠️ Нет активных соединений для отправки команды")
            return False

        message = json.dumps(payload)
        logger.debug(f"📤 Отправка: {payload}")

        async def send():
            disconnected = []
            for ws in list(self._clients):
                try:
                    await ws.send(message)
                except (ConnectionClosed, InvalidState):
                    logger.warning("🔌 Клиент отключился при отправке")
                    disconnected.append(ws)
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки: {e}")
                    disconnected.append(ws)

            for ws in disconnected:
                self._clients.discard(ws)

        future = asyncio.run_coroutine_threadsafe(send(), self._ws_loop)
        future.add_done_callback(self._log_send_result)
        return True

    def _log_send_result(self, future):
        """Логирование результата отправки"""
        try:
            future.result()
        except asyncio.CancelledError:
            logger.debug("📤 Отправка отменена")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке: {e}")

    # ======================== КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ========================
    def _cmd_play_pause(self):
        """Переключение пауза/плей с оптимистичным обновлением"""
        self.is_playing = not self.is_playing
        self._update_play_icon()
        self.progress.set_playing(self.is_playing)
        self._send_command({"request": "playerInteraction"})
        # Подтверждение придёт от плагина через ~100мс

    def _cmd_like(self):
        """Переключение лайка с оптимистичным обновлением"""
        self.is_liked = not self.is_liked
        self._update_like_icon()
        self._send_command({"request": "likeInteraction"})

    def _cmd_track(self, delta: int):
        """Переключение трека"""
        cmd = "moveForward" if delta > 0 else "moveBackward"
        self._send_command({"request": cmd})
        # Сброс прогресса для нового трека
        self.progress.reset()
        QTimer.singleShot(300, self._force_refresh)

    def _force_refresh(self):
        """Принудительный запрос всех данных"""
        for cmd in ["trackInfo", "coverImage", "playingState", "likeState"]:
            self._send_command({"request": cmd})

    # ======================== ОБНОВЛЕНИЕ UI ========================
    def _handle_ws_message(self, data: dict):
        """Обработка сообщения от плагина"""
        request = data.get("request")
        response = data.get("response")

        logger.debug(f"📥 Ответ: {request} = {response}")

        if request == "trackInfo" and response:
            self._update_track_info(str(response))

        elif request == "coverImage" and response:
            url = str(response)
            if url != self.current_cover_url:
                self.current_cover_url = url
                self._load_cover_async(url)

        elif request == "playingState":
            new_state = (response == 0) if response is not None else False
            if self.is_playing != new_state:
                self.is_playing = new_state
                self._update_play_icon()
                self.progress.set_playing(self.is_playing)

        elif request == "likeState":
            new_state = (response == 1) if response is not None else False
            if self.is_liked != new_state:
                self.is_liked = new_state
                self._update_like_icon()

    def _update_track_info(self, response: str):
        """Обновление названия трека и исполнителя"""
        try:
            parts = response.split(";;")
            track = parts[0].strip() if parts and parts[0].strip() else "Неизвестный трек"
            artist = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Неизвестный исполнитель"

            self.title_fade.change(track)
            self.artist_fade.change(artist)
            self.setWindowTitle(f"EvilDesk | {track}")

        except Exception as e:
            logger.error(f"❌ Ошибка обновления трека: {e}")

    def _update_play_icon(self):
        """Обновление иконки плей/пауза"""
        if self.play_btn:
            icon = self.icons["pause"] if self.is_playing else self.icons["play"]
            if not icon.isNull():
                self.play_btn.setIcon(icon)

    def _update_like_icon(self):
        """Обновление иконки лайка"""
        if self.like_btn:
            icon = self.icons["like_active"] if self.is_liked else self.icons["like"]
            if not icon.isNull():
                self.like_btn.setIcon(icon)

    def _update_connection_indicator(self, connected: bool):
        """Обновление индикатора подключения (опционально)"""
        status = "✅ Подключено" if connected else "⏳ Ожидание..."
        logger.info(f"🔗 Статус: {status}")
        # Можно добавить визуальный индикатор в UI при необходимости

    # ======================== ОБЛОЖКА И ФОН ========================
    def _load_cover_async(self, url: str):
        if url in self.cover_cache:
            img_bytes, colors = self.cover_cache[url]
            bridge.cover_loaded.emit(url, img_bytes)
            return

        def download():
            try:
                download_url = url
                if download_url.startswith(
                        "http://") and "localhost" not in download_url and "127.0.0.1" not in download_url:
                    download_url = download_url.replace("http://", "https://", 1)

                # ✅ Используем сессию вместо requests.get()
                response = self.http_session.get(download_url, timeout=6)
                response.raise_for_status()
                img_bytes = response.content

                colors = ThemeEngine.extract_dominant_colors(img_bytes)
                self.cover_cache[url] = (img_bytes, colors)

                bridge.cover_loaded.emit(url, img_bytes)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка загрузки обложки: {e}")
                self._show_placeholder_cover()

        threading.Thread(target=download, daemon=True).start()

    def _apply_cover_image(self, url: str, img_bytes: bytes):
        """Применение обложки + мгновенное обновление фона"""
        if url != self.current_cover_url:
            return

        # 1. Основная обложка
        cover_pixmap = QPixmap()
        if not cover_pixmap.loadFromData(img_bytes):
            logger.error("❌ Не удалось декодировать изображение обложки")
            return

        scaled_cover = cover_pixmap.scaled(
            *CONFIG["cover_size"],
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )

        # Анимация появления
        if self.cover_label.pixmap() is None:
            self.cover_fade.set_opacity(0)
            self.cover_label.setPixmap(scaled_cover)
            self.cover_fade.fade(0, 1)
            self.progress.show()
        else:
            def on_fade_out():
                try:
                    self.cover_fade.anim.finished.disconnect(on_fade_out)
                except:
                    pass
                self.cover_label.setPixmap(scaled_cover)
                self.cover_fade.fade(0, 1)

            self.cover_fade.fade(1, 0, on_fade_out)

        # 2. ФОН: Обновляем СРАЗУ, без задержек
        self._current_cover_bytes = img_bytes  # Сохраняем для ресайза

        bg_pixmap = QPixmap()
        bg_pixmap.loadFromData(img_bytes)
        # Увеличиваем на 20%, чтобы размытие не оставляло тёмных краёв
        bg_pixmap = bg_pixmap.scaled(
            int(self.width() * 1.2), int(self.height() * 1.2),
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation
        )
        self.bg_label.setPixmap(bg_pixmap)

    def _show_placeholder_cover(self):
        """Показ заглушки при ошибке загрузки"""
        pixmap = QPixmap(*CONFIG["cover_size"])
        pixmap.fill(QColor(42, 42, 42))
        self.cover_label.setPixmap(pixmap)
        self.cover_fade.set_opacity(1)

    # ======================== ПРОГРЕСС-БАР ========================
    def _on_progress_update(self, progress: float):
        """Обработчик обновления прогресса (опционально)"""
        # Здесь можно добавить синхронизацию с реальным временем трека
        # если плагин начнёт отправлять currentTime/duration
        pass

    # ======================== ПОЛЛИНГ ========================
    def _start_polling(self):
        """Запуск периодического опроса состояния"""
        self.poll_timer.setInterval(CONFIG["poll_interval_ms"])
        self.poll_timer.timeout.connect(self._force_refresh)
        self.poll_timer.start()

    # ======================== СОБЫТИЯ ========================
    def resizeEvent(self, event):
        """Корректная обработка изменения размера окна"""
        super().resizeEvent(event)

        # Обновляем геометрию фона
        self.bg_label.setGeometry(0, 0, self.width(), self.height())
        self._update_edge_shadow()

        # Перерисовываем фон, если обложка уже загружена
        if self._current_cover_bytes:
            self._apply_cover_image(self.current_cover_url, self._current_cover_bytes)

    def mousePressEvent(self, event: QMouseEvent):
        """Обработка нажатия мыши (для выхода)"""
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            # Выход при клике в нижний левый угол (удержание 3.5 сек)
            if pos.x() < 80 and pos.y() > self.height() - 80:
                self.exit_timer.start(3500)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """Обработка отпускания мыши"""
        if self.exit_timer.isActive():
            self.exit_timer.stop()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        """Обработка закрытия окна"""
        self._cleanup()
        super().closeEvent(event)

    # ======================== ЗАВЕРШЕНИЕ ========================
    def _cleanup(self):
        """Корректное завершение работы"""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True

        logger.info("🛑 Завершение работы...")

        # Остановка таймеров
        self.poll_timer.stop()
        self.exit_timer.stop()

        # Закрытие WebSocket
        self._request_ws_shutdown()

        # Ожидание завершения потока
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2)

    @Slot()
    def safe_exit(self):
        """Безопасный выход из приложения"""
        self._cleanup()
        QApplication.quit()


# ======================== ТОЧКА ВХОДА ========================
def main():
    """Точка входа в приложение"""
    parser = argparse.ArgumentParser(description="EvilDesk Player")
    parser.add_argument("--host", default=CONFIG["ws_host"],
                        help="Host для WebSocket-сервера")
    parser.add_argument("--port", type=int, default=CONFIG["ws_port"],
                        help="Port для WebSocket-сервера")
    parser.add_argument("--log", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default="INFO", help="Уровень логирования")

    args = parser.parse_args()

    # Настройка логирования
    log_level = getattr(logging, args.log)
    setup_logging(log_level)

    # Инициализация QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("EvilDesk Player")
    app.setOrganizationName("EvilDesk")

    # Загрузка шрифтов
    try:
        QFontDatabase.addApplicationFont("fonts/MontserratAlternates-Medium.ttf")
    except:
        logger.warning("⚠️ Не удалось загрузить шрифт Montserrat")

    # Запуск главного окна
    player = MusicPlayer(host=args.host, port=args.port)

    # Запуск event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()