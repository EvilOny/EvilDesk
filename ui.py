import pygame
import sys
from PIL import Image
import numpy as np

pygame.init()

# ---------- SETTINGS ----------
SCREEN_W = 480
SCREEN_H = 800
FPS = 60

# Paths
COVER_PATH = "cover.jpg"   # 500x500 or any size
FONT_PATH = None           # default pygame font

# ---------- FUNCTIONS ----------

def dominant_color(path):
    img = Image.open(path).resize((50, 50))
    arr = np.array(img).reshape((-1, 3))
    avg = arr.mean(axis=0)
    return tuple(avg.astype(int))


def glass_rect(surface, rect, alpha=140, blur_strength=6):
    """Simple fake blur: capture region, scale down, scale up, draw with alpha."""
    x, y, w, h = rect
    sub = surface.subsurface(rect).copy()

    small = pygame.transform.smoothscale(sub, (w//blur_strength, h//blur_strength))
    blurred = pygame.transform.smoothscale(small, (w, h))

    blurred.set_alpha(alpha)
    surface.blit(blurred, (x, y))


# ---------- INIT ----------
screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
clock = pygame.time.Clock()

dominant = dominant_color(COVER_PATH)
bg_color = dominant

cover_raw = Image.open(COVER_PATH)
cover_size = min(SCREEN_W * 0.9, SCREEN_H * 0.9)
cover_size = int(cover_size)

cover_raw = cover_raw.resize((cover_size, cover_size))
cover = pygame.image.fromstring(cover_raw.tobytes(), cover_raw.size, cover_raw.mode)

cover_x = (SCREEN_W - cover_size) // 2
cover_y = (SCREEN_H - cover_size) // 2 - 40

# ---------- BUTTONS ----------

# Icons may be replaced with any image files
def draw_icon(size=60, color=(255,255,255), shape="play"):
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    if shape == "play":
        pygame.draw.polygon(surf, color, [(20,10),(20,50),(50,30)])
    elif shape == "pause":
        pygame.draw.rect(surf, color, (15,10,10,40))
        pygame.draw.rect(surf, color, (35,10,10,40))
    elif shape == "next":
        pygame.draw.polygon(surf, color, [(15,10),(15,50),(40,30)])
        pygame.draw.rect(surf, color, (45,10,5,40))
    elif shape == "prev":
        pygame.draw.polygon(surf, color, [(45,10),(45,50),(20,30)])
        pygame.draw.rect(surf, color, (15,10,5,40))
    elif shape == "heart":
        pygame.draw.polygon(surf, color, [(30,15),(45,25),(30,45),(15,25)])
    return surf

ICON_SIZE = 60

btn_prev  = draw_icon(ICON_SIZE, shape="prev")
btn_play  = draw_icon(ICON_SIZE, shape="play")
btn_next  = draw_icon(ICON_SIZE, shape="next")
btn_like  = draw_icon(ICON_SIZE, shape="heart")

panel_h = 120
panel_y = SCREEN_H - panel_h

# Buttons positions
gap = SCREEN_W // 5

btn_prev_pos = (gap - ICON_SIZE//2, panel_y + panel_h//2 - ICON_SIZE//2)
btn_play_pos = (2*gap - ICON_SIZE//2, panel_y + panel_h//2 - ICON_SIZE//2)
btn_next_pos = (3*gap - ICON_SIZE//2, panel_y + panel_h//2 - ICON_SIZE//2)
btn_like_pos = (4*gap - ICON_SIZE//2, panel_y + panel_h//2 - ICON_SIZE//2)

buttons = {
    "prev": pygame.Rect(btn_prev_pos[0], btn_prev_pos[1], ICON_SIZE, ICON_SIZE),
    "play": pygame.Rect(btn_play_pos[0], btn_play_pos[1], ICON_SIZE, ICON_SIZE),
    "next": pygame.Rect(btn_next_pos[0], btn_next_pos[1], ICON_SIZE, ICON_SIZE),
    "like": pygame.Rect(btn_like_pos[0], btn_like_pos[1], ICON_SIZE, ICON_SIZE),
}

is_playing = False
liked = False


# ---------- MAIN LOOP ----------
while True:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            pygame.quit()
            sys.exit()

        if event.type == pygame.MOUSEBUTTONDOWN:
            mx, my = pygame.mouse.get_pos()

            if buttons["prev"].collidepoint(mx,my):
                print("Previous track")

            if buttons["play"].collidepoint(mx,my):
                is_playing = not is_playing
                btn_play = draw_icon(ICON_SIZE, shape=("pause" if is_playing else "play"))

            if buttons["next"].collidepoint(mx,my):
                print("Next track")

            if buttons["like"].collidepoint(mx,my):
                liked = not liked
                col = (255,50,50) if liked else (255,255,255)
                btn_like = draw_icon(ICON_SIZE, color=col, shape="heart")

    # Draw background
    screen.fill(bg_color)

    # Draw cover
    screen.blit(cover, (cover_x, cover_y))

    # Glass panel
    glass_rect(screen, (0, panel_y, SCREEN_W, panel_h), alpha=180)

    # Draw buttons
    screen.blit(btn_prev, btn_prev_pos)
    screen.blit(btn_play, btn_play_pos)
    screen.blit(btn_next, btn_next_pos)
    screen.blit(btn_like, btn_like_pos)

    pygame.display.flip()
    clock.tick(FPS)
