import sys
import sdl2
import sdl2.ext
from PIL import Image
import numpy as np


# -------------------------------------------------------
# Получение доминирующего цвета изображения
# -------------------------------------------------------
def get_dominant_color(path):
    img = Image.open(path).resize((50, 50))
    arr = np.array(img).reshape(-1, 3)
    return tuple(arr.mean(axis=0).astype(int))


# -------------------------------------------------------
# Кнопка
# -------------------------------------------------------
class Button:
    def __init__(self, texture, x, y, w, h, callback):
        self.texture = texture
        self.rect = sdl2.SDL_Rect(x, y, w, h)
        self.callback = callback

    def draw(self, renderer):
        renderer.copy(self.texture, dstrect=self.rect)

    def click(self, x, y):
        if (self.rect.x <= x <= self.rect.x + self.rect.w and
            self.rect.y <= y <= self.rect.y + self.rect.h):
            self.callback()
            return True
        return False


# -------------------------------------------------------
# Основной UI
# -------------------------------------------------------
class PlayerUI:
    def __init__(self, cover_path="cover.jpg"):
        sdl2.ext.init()

        self.window = sdl2.ext.Window(
            "Music Player",
            size=(800, 480),
            flags=sdl2.SDL_WINDOW_FULLSCREEN
        )
        self.window.show()

        self.renderer = sdl2.ext.Renderer(self.window)
        self.factory = sdl2.ext.TextureSpriteFactory(self.renderer)

        # Обложка и доминирующий цвет
        self.cover_path = cover_path
        self.cover_tex = self.factory.from_image(cover_path)
        self.dominant_color = get_dominant_color(cover_path)

        # Что сейчас показывает кнопка play/pause
        self.is_playing = False

        # Загружаем кнопки
        self.btn_prev = self._load_button("icons/prev.png")
        self.btn_play = self._load_button("icons/play.png")
        self.btn_pause = self._load_button("icons/pause.png")
        self.btn_next = self._load_button("icons/next.png")
        self.btn_like = self._load_button("icons/like.png")

        # Создаем объекты кнопок
        self.buttons = self._setup_buttons()

    # -------------------------------------------------------
    def _load_button(self, path):
        return self.factory.from_image(path)

    # -------------------------------------------------------
    def _setup_buttons(self):
        w, h = self.window.size
        panel_h = 90

        # размеры кнопок
        bw = 64
        bh = 64
        center_y = h - panel_h // 2 - bh // 2

        spacing = 120
        cx = w // 2

        def on_prev():
            print("⏮ prev clicked")

        def on_play_pause():
            self.is_playing = not self.is_playing
            print("⏯ play/pause:", self.is_playing)

        def on_next():
            print("⏭ next clicked")

        def on_like():
            print("❤ like clicked")

        buttons = [
            Button(self.btn_prev.texture, cx - spacing - bw // 2, center_y, bw, bh, on_prev),
            Button(self.btn_play.texture, cx - bw // 2, center_y, bw, bh, on_play_pause),
            Button(self.btn_next.texture, cx + spacing - bw // 2, center_y, bw, bh, on_next),
            Button(self.btn_like.texture, w - bw - 20, center_y, bw, bh, on_like)
        ]
        return buttons

    # -------------------------------------------------------
    def draw_background(self):
        # Мягкий фон по доминирующему цвету
        r, g, b = self.dominant_color
        self.renderer.clear(sdl2.SDL_Color(r, g, b))

    # -------------------------------------------------------
    def draw_cover(self):
        w, h = self.window.size

        # делаем квадратный размер
        size = min(w, int(h * 0.8))

        x = (w - size) // 2
        y = (h - size) // 2 - 20

        rect = sdl2.SDL_Rect(x, y, size, size)
        self.renderer.copy(self.cover_tex.texture, dstrect=rect)

    # -------------------------------------------------------
    def draw_panel(self):
        w, h = self.window.size
        panel_h = 90

        # «Liquid Glass» — имитация (полупрозрачный градиент)
        for i in range(panel_h):
            alpha = int(150 + 80 * (i / panel_h))  # сверху прозрачнее, снизу плотнее
            rect = sdl2.SDL_Rect(0, h - panel_h + i, w, 1)
            self.renderer.fill(rect, sdl2.SDL_Color(40, 40, 40, alpha))

    # -------------------------------------------------------
    def draw_buttons(self):
        for b in self.buttons:
            if b is self.buttons[1]:  # кнопка play/pause
                # Заменяем картинку динамически
                if self.is_playing:
                    self.renderer.copy(
                        self.btn_pause.texture,
                        dstrect=b.rect
                    )
                else:
                    self.renderer.copy(
                        self.btn_play.texture,
                        dstrect=b.rect
                    )
            else:
                b.draw(self.renderer)

    # -------------------------------------------------------
    def run(self):
        running = True

        while running:
            for event in sdl2.ext.get_events():

                if event.type == sdl2.SDL_QUIT:
                    running = False

                # клик
                if event.type == sdl2.SDL_MOUSEBUTTONDOWN:
                    mx, my = event.button.x, event.button.y
                    for b in self.buttons:
                        b.click(mx, my)

            # Рендеринг
            self.draw_background()
            self.draw_cover()
            self.draw_panel()
            self.draw_buttons()

            self.renderer.present()

        return 0


# -------------------------------------------------------
if __name__ == "__main__":
    ui = PlayerUI()
    sys.exit(ui.run())
