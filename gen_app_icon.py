"""Generates the final app icon (speedometer/gauge), matching the dashboard's
own brand gradient (--accent #38d6c0 -> --accent-dim #1f7c71, see
templates/index.html's .brand CSS), at high resolution with supersampling for
clean edges, then exports:
  - assets/app_icon_full.png / assets/app.ico  (gradient bg + white gauge --
    used for the PyInstaller exe icon AND the web app's favicon)
  - static/favicon.ico                          (Flask auto-serves /static)
  - assets/icon_glyph_white.png                  (transparent bg, white gauge
    only -- for embedding inside the nav-bar's existing gradient badge)
Safe to re-run any time to regenerate from scratch.
"""
import math
import os
from PIL import Image, ImageDraw

SS = 4  # supersampling factor for clean anti-aliased edges
BASE = 256
SIZE = BASE * SS

ACCENT = (0x38, 0xd6, 0xc0)       # --accent (teal)
ACCENT_DIM = (0x1f, 0x7c, 0x71)   # --accent-dim (dark teal)
WHITE = (255, 255, 255, 255)

os.makedirs("assets", exist_ok=True)
os.makedirs("static", exist_ok=True)


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def diagonal_gradient(size, c1, c2):
    """135deg linear gradient, matching CSS linear-gradient(135deg, c1, c2)."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = lerp(c1, c2, t)
    return img


def draw_gauge(draw: ImageDraw.ImageDraw, cx, cy, r, color, needle_angle_deg=-40):
    """A clean dial: 270-degree arc, tick marks, a needle, and a hub."""
    arc_w = max(1, int(r * 0.11))
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=135, end=405, fill=color, width=arc_w)
    # Tick marks around the dial
    for ang in range(135, 406, 27):
        rad = math.radians(ang)
        x1, y1 = cx + math.cos(rad) * (r - arc_w * 1.6), cy + math.sin(rad) * (r - arc_w * 1.6)
        x2, y2 = cx + math.cos(rad) * (r - arc_w * 0.3), cy + math.sin(rad) * (r - arc_w * 0.3)
        draw.line([x1, y1, x2, y2], fill=color, width=max(1, arc_w // 3))
    # Needle, pointing toward the "high load" end of the dial
    rad = math.radians(needle_angle_deg)
    needle_len = r * 0.78
    tip = (cx + math.cos(rad) * needle_len, cy + math.sin(rad) * needle_len)
    back_rad1, back_rad2 = math.radians(needle_angle_deg + 150), math.radians(needle_angle_deg - 150)
    back_len = r * 0.16
    b1 = (cx + math.cos(back_rad1) * back_len, cy + math.sin(back_rad1) * back_len)
    b2 = (cx + math.cos(back_rad2) * back_len, cy + math.sin(back_rad2) * back_len)
    draw.polygon([tip, b1, b2], fill=color)
    hub_r = r * 0.14
    draw.ellipse([cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r], fill=color)


# ---- 1. Full icon: gradient rounded-square background + white gauge --------
bg = diagonal_gradient(SIZE, ACCENT, ACCENT_DIM).convert("RGBA")
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [SIZE * 0.03, SIZE * 0.03, SIZE * 0.97, SIZE * 0.97], radius=SIZE * 0.22, fill=255)
full = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
full.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(full)
draw_gauge(d, SIZE * 0.5, SIZE * 0.53, SIZE * 0.30, WHITE)
full = full.resize((BASE, BASE), Image.LANCZOS)
full.save("assets/app_icon_full.png")
full.save("assets/app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
full.save("static/favicon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

# ---- 2. Glyph-only: transparent bg, white gauge (for the in-app nav badge) --
glyph = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
dg = ImageDraw.Draw(glyph)
draw_gauge(dg, SIZE * 0.5, SIZE * 0.53, SIZE * 0.42, WHITE)
glyph = glyph.resize((BASE, BASE), Image.LANCZOS)
glyph.save("assets/icon_glyph_white.png")
glyph.save("static/icon_glyph.png")

print("done")
