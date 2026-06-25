"""Generate branded tweet images using Pillow."""
import io
import logging
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

# Image dimensions (Twitter 16:9 optimal)
W, H = 1200, 628

# Colour palette
C_BG        = (10, 10, 15)
C_SURFACE   = (23, 23, 34)
C_PURPLE    = (168, 85, 247)
C_INDIGO    = (99, 102, 241)
C_PINK      = (236, 72, 153)
C_WHITE     = (255, 255, 255)
C_MUTED     = (161, 161, 170)
C_DIM       = (113, 113, 122)
C_GREEN     = (34, 197, 94)
C_YELLOW    = (245, 158, 11)

CATEGORY_COLORS = {
    "Steuern":      (245, 158, 11),
    "Finanzen":     (99, 102, 241),
    "Recht":        (168, 85, 247),
    "Wirtschaft":   (34, 197, 94),
    "Einwanderung": (6, 182, 212),
    "Abstimmung":   (236, 72, 153),
    "Regulierung":  (239, 68, 68),
    "Immobilien":   (249, 115, 22),
    "Arbeit":       (20, 184, 166),
    "Startup":      (132, 204, 22),
}
DEFAULT_CAT_COLOR = (168, 85, 247)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
]


def _load_font(candidates, size):
    from PIL import ImageFont
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_rounded_rect(draw, xy, radius, fill):
    from PIL import ImageDraw
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.ellipse([x0, y0, x0 + radius*2, y0 + radius*2], fill=fill)
    draw.ellipse([x1 - radius*2, y0, x1, y0 + radius*2], fill=fill)
    draw.ellipse([x0, y1 - radius*2, x0 + radius*2, y1], fill=fill)
    draw.ellipse([x1 - radius*2, y1 - radius*2, x1, y1], fill=fill)


def _wrap_text(text, font, max_width, draw):
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    current = []
    for word in words:
        test = ' '.join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > max_width and current:
            lines.append(' '.join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(' '.join(current))
    return lines


def generate_post_image(item: dict) -> bytes:
    """Return PNG image bytes for a tweet post item."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("Pillow not installed — cannot generate image")
        return b""

    title    = (item.get("title") or "").strip()
    category = (item.get("category") or "News").strip()
    source   = (item.get("source_id") or "").strip()
    cat_color = CATEGORY_COLORS.get(category, DEFAULT_CAT_COLOR)

    # ── Canvas ─────────────────────────────────────────────────────────────
    img  = Image.new("RGB", (W, H), C_BG)
    draw = ImageDraw.Draw(img)

    # ── Radial purple glow top-left ────────────────────────────────────────
    from PIL import Image as PILImage
    glow = PILImage.new("RGB", (W, H), C_BG)
    gd   = ImageDraw.Draw(glow)
    for r in range(420, 0, -4):
        alpha = int(18 * (1 - r / 420))
        col = tuple(min(255, c + alpha) for c in C_BG)
        col = (
            min(255, C_BG[0] + int((C_PURPLE[0] - C_BG[0]) * alpha / 255)),
            min(255, C_BG[1] + int((C_PURPLE[1] - C_BG[1]) * alpha / 255)),
            min(255, C_BG[2] + int((C_PURPLE[2] - C_BG[2]) * alpha / 255)),
        )
        gd.ellipse([-r + 180, -r + 80, r + 180, r + 80], fill=col)
    img = PILImage.blend(img, glow, alpha=0.55)
    draw = ImageDraw.Draw(img)

    # ── Left accent bar ────────────────────────────────────────────────────
    for x in range(5):
        alpha = int(255 * (1 - x / 5))
        bar_col = tuple(int(C_PURPLE[i] * alpha / 255 + C_BG[i] * (1 - alpha/255)) for i in range(3))
        draw.rectangle([x, 0, x, H], fill=bar_col)
    draw.rectangle([0, 0, 4, H], fill=C_PURPLE)

    # ── Bottom gradient bar ────────────────────────────────────────────────
    for x in range(W):
        t = x / W
        r = int(C_INDIGO[0] * (1-t) + C_PINK[0] * t)
        g = int(C_INDIGO[1] * (1-t) + C_PINK[1] * t)
        b = int(C_INDIGO[2] * (1-t) + C_PINK[2] * t)
        draw.line([(x, H-4), (x, H)], fill=(r, g, b))

    # ── Fonts ──────────────────────────────────────────────────────────────
    f_logo    = _load_font(FONT_CANDIDATES, 22)
    f_cat     = _load_font(FONT_CANDIDATES, 18)
    f_title   = _load_font(FONT_CANDIDATES, 52)
    f_title_s = _load_font(FONT_CANDIDATES, 44)
    f_source  = _load_font(FONT_REGULAR_CANDIDATES, 20)
    f_brand   = _load_font(FONT_CANDIDATES, 20)

    # ── Top bar ────────────────────────────────────────────────────────────
    # Logo
    draw.text((28, 28), "🇨🇭 SCHWEIZINTEL", font=f_logo, fill=C_WHITE)

    # Category badge (top right)
    cat_text = category.upper()
    cat_bbox = draw.textbbox((0, 0), cat_text, font=f_cat)
    cat_w    = cat_bbox[2] - cat_bbox[0] + 28
    cat_h    = cat_bbox[3] - cat_bbox[1] + 14
    cat_x    = W - cat_w - 32
    cat_y    = 22
    _draw_rounded_rect(draw, (cat_x, cat_y, cat_x + cat_w, cat_y + cat_h), 8,
                       tuple(int(c * 0.25) for c in cat_color))
    draw.rectangle([(cat_x, cat_y), (cat_x + 3, cat_y + cat_h)], fill=cat_color)
    draw.text((cat_x + 16, cat_y + 7), cat_text, font=f_cat, fill=cat_color)

    # ── Divider ────────────────────────────────────────────────────────────
    draw.rectangle([28, 76, W - 32, 78], fill=C_SURFACE)

    # ── Title ──────────────────────────────────────────────────────────────
    TITLE_X     = 38
    TITLE_Y     = 110
    TITLE_MAX_W = W - TITLE_X - 40
    MAX_LINES   = 3

    lines = _wrap_text(title, f_title, TITLE_MAX_W, draw)
    font_used = f_title
    if len(lines) > MAX_LINES:
        lines = _wrap_text(title, f_title_s, TITLE_MAX_W, draw)
        font_used = f_title_s
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1].rstrip()
        # Trim last line to fit with ellipsis
        while lines[-1]:
            test = lines[-1] + "…"
            bb = draw.textbbox((0, 0), test, font=font_used)
            if bb[2] - bb[0] <= TITLE_MAX_W:
                lines[-1] = test
                break
            lines[-1] = lines[-1][:-1].rstrip()

    line_h = draw.textbbox((0, 0), "Ag", font=font_used)[3] + 12
    for i, line in enumerate(lines):
        draw.text((TITLE_X, TITLE_Y + i * line_h), line, font=font_used, fill=C_WHITE)

    # ── Bottom section ─────────────────────────────────────────────────────
    BOTTOM_Y = H - 64

    # Separator
    draw.rectangle([28, BOTTOM_Y - 12, W - 32, BOTTOM_Y - 10], fill=C_SURFACE)

    # Source (left)
    draw.text((38, BOTTOM_Y), source, font=f_source, fill=C_MUTED)

    # Branding (right)
    brand = "@schweizintel"
    bb = draw.textbbox((0, 0), brand, font=f_brand)
    brand_w = bb[2] - bb[0]
    draw.text((W - brand_w - 32, BOTTOM_Y), brand, font=f_brand, fill=C_DIM)

    # ── Serialize ──────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
