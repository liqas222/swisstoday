"""Generate branded tweet images — Schweiz Intel dark/noir style."""
import io
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

W, H = 1200, 628

# Colour palette — matches X profile (dark noir + red accent)
C_BG      = (6, 7, 10)
C_SURFACE = (14, 15, 20)
C_RED     = (204, 10, 10)
C_RED_HI  = (239, 40, 40)
C_WHITE   = (255, 255, 255)
C_MUTED   = (160, 163, 175)
C_DIM     = (90, 92, 102)
C_GREY    = (30, 32, 40)

CATEGORY_COLORS = {
    "Steuern":      (220, 38, 38),
    "Finanzen":     (220, 38, 38),
    "Recht":        (185, 28, 28),
    "Wirtschaft":   (185, 28, 28),
    "Einwanderung": (200, 30, 30),
    "Abstimmung":   (220, 38, 38),
    "Regulierung":  (200, 20, 20),
    "Immobilien":   (180, 30, 30),
    "Arbeit":       (200, 25, 25),
    "Startup":      (210, 35, 35),
}
DEFAULT_CAT_COLOR = (204, 10, 10)

FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
FONT_REG = [
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


def _wrap_text(text, font, max_width, draw):
    words = text.split()
    lines, current = [], []
    for word in words:
        test = ' '.join(current + [word])
        if draw.textbbox((0, 0), test, font=font)[2] > max_width and current:
            lines.append(' '.join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(' '.join(current))
    return lines


def _draw_mountain_silhouette(draw, ox, oy, scale=1.0, alpha_mult=1.0):
    """Draw a subtle Matterhorn-like triangle silhouette."""
    # Normalised Matterhorn peak points (0-1 range)
    pts = [
        (0.0, 1.0), (0.12, 0.72), (0.22, 0.60), (0.28, 0.38),
        (0.32, 0.15), (0.35, 0.05), (0.38, 0.15), (0.42, 0.32),
        (0.48, 0.48), (0.56, 0.62), (0.68, 0.74), (0.80, 0.85),
        (1.0, 1.0),
    ]
    mw = int(420 * scale)
    mh = int(320 * scale)
    scaled = [(ox + int(x * mw), oy + int(y * mh)) for x, y in pts]
    # Draw filled silhouette very darkly
    try:
        from PIL import ImageDraw as _ID
        # Use polygon with very dark colour
        col = (16, 17, 24)
        draw.polygon(scaled, fill=col)
        # Outline with slight red tint
        draw.line(scaled, fill=(50, 10, 10), width=1)
    except Exception:
        pass


def _draw_network_dots(draw, seed=42):
    """Draw faint network nodes like the X banner background."""
    import random
    rng = random.Random(seed)
    nodes = [(rng.randint(50, W-50), rng.randint(50, H-50)) for _ in range(28)]
    # Edges
    for i, (x1, y1) in enumerate(nodes):
        for x2, y2 in nodes[i+1:]:
            dist = math.hypot(x2-x1, y2-y1)
            if dist < 220:
                alpha = max(0, int(18 * (1 - dist/220)))
                col = (alpha//3, alpha//3, alpha//3)
                draw.line([(x1, y1), (x2, y2)], fill=col, width=1)
    # Nodes — a few red, rest grey
    for i, (x, y) in enumerate(nodes):
        if i % 5 == 0:
            # Red node
            draw.ellipse([x-3, y-3, x+3, y+3], fill=(160, 10, 10))
            draw.ellipse([x-6, y-6, x+6, y+6], outline=(80, 5, 5))
        else:
            draw.ellipse([x-2, y-2, x+2, y+2], fill=(40, 42, 52))


def _draw_rounded_rect(draw, xy, radius, fill, outline=None):
    x0, y0, x1, y1 = xy
    r = radius
    draw.rectangle([x0+r, y0, x1-r, y1], fill=fill)
    draw.rectangle([x0, y0+r, x1, y1-r], fill=fill)
    for cx, cy in [(x0+r, y0+r), (x1-r, y0+r), (x0+r, y1-r), (x1-r, y1-r)]:
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=fill)
    if outline:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r, outline=outline, width=1)


def generate_post_image(item: dict) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("Pillow not installed")
        return b""

    title    = (item.get("title") or "").strip()
    category = (item.get("category") or "").strip()
    source   = (item.get("source_id") or "").strip().upper()
    cat_col  = CATEGORY_COLORS.get(category, DEFAULT_CAT_COLOR)

    img  = Image.new("RGB", (W, H), C_BG)
    draw = ImageDraw.Draw(img)

    # ── Background: faint mountain silhouette (right side) ─────────────────
    _draw_mountain_silhouette(draw, ox=W-460, oy=H-300, scale=1.0)

    # ── Network dots (subtle) ───────────────────────────────────────────────
    _draw_network_dots(draw)

    # ── Dark vignette overlay on left (so text is readable) ────────────────
    for x in range(700):
        t = max(0.0, 1.0 - x / 700)
        opacity = int(200 * t)
        col = tuple(int(C_BG[i] * opacity / 255 + draw._image.getpixel((x, H//2))[i] * (1 - opacity/255)) for i in range(3))
    # Simple dark overlay rectangle left 2/3
    overlay = Image.new("RGB", (W, H), C_BG)
    ov_draw = ImageDraw.Draw(overlay)
    # Gradient left → transparent
    for x in range(W):
        t = min(1.0, max(0.0, 1.0 - (x - 400) / 400))
        alpha = int(220 * t)
        if alpha > 0:
            ov_draw.line([(x, 0), (x, H)], fill=C_BG)
    img = Image.blend(img, overlay, alpha=0.72)
    draw = ImageDraw.Draw(img)

    # ── Subtle red glow top-left ────────────────────────────────────────────
    from PIL import Image as PILImage
    glow = PILImage.new("RGB", (W, H), C_BG)
    gd = ImageDraw.Draw(glow)
    for r in range(300, 0, -6):
        a = int(22 * (1 - r/300))
        col = (
            min(255, C_BG[0] + int((C_RED[0]-C_BG[0]) * a / 255)),
            min(255, C_BG[1] + int((C_RED[1]-C_BG[1]) * a / 255)),
            min(255, C_BG[2] + int((C_RED[2]-C_BG[2]) * a / 255)),
        )
        gd.ellipse([-r+100, -r+60, r+100, r+60], fill=col)
    img = PILImage.blend(img, glow, alpha=0.5)
    draw = ImageDraw.Draw(img)

    # ── Left red accent bar ─────────────────────────────────────────────────
    draw.rectangle([0, 0, 4, H], fill=C_RED)

    # ── Bottom accent bar: dark red ─────────────────────────────────────────
    for x in range(W):
        t = x / W
        r = int(120 * (1-t) + 80 * t)
        draw.line([(x, H-4), (x, H)], fill=(r, 8, 8))

    # ── Fonts ───────────────────────────────────────────────────────────────
    f_logo  = _load_font(FONT_BOLD, 19)
    f_cat   = _load_font(FONT_BOLD, 17)
    f_title = _load_font(FONT_BOLD, 54)
    f_title_s = _load_font(FONT_BOLD, 44)
    f_src   = _load_font(FONT_REG, 19)
    f_brand = _load_font(FONT_BOLD, 19)

    # ── Top: "Si" logo mark + brand name ───────────────────────────────────
    # Circle logo
    cx, cy, cr = 42, 44, 20
    draw.ellipse([cx-cr, cy-cr, cx+cr, cy+cr], fill=C_GREY)
    draw.ellipse([cx-cr, cy-cr, cx+cr, cy+cr], outline=C_RED, width=1)
    si_bb = draw.textbbox((0,0), "Si", font=f_logo)
    si_w, si_h = si_bb[2]-si_bb[0], si_bb[3]-si_bb[1]
    draw.text((cx - si_w//2, cy - si_h//2 - 1), "Si", font=f_logo, fill=C_WHITE)
    # Brand name
    draw.text((72, 34), "SCHWEIZ INTEL", font=f_logo, fill=C_WHITE)

    # ── Category badge top-right ────────────────────────────────────────────
    if category:
        cat_text = category.upper()
        cb = draw.textbbox((0,0), cat_text, font=f_cat)
        cw = cb[2]-cb[0]+26
        ch = cb[3]-cb[1]+12
        cx2 = W - cw - 28
        cy2 = 24
        _draw_rounded_rect(draw, (cx2, cy2, cx2+cw, cy2+ch), 6,
                           fill=tuple(int(c*0.18) for c in cat_col))
        draw.rectangle([cx2, cy2, cx2+3, cy2+ch], fill=cat_col)
        draw.text((cx2+14, cy2+6), cat_text, font=f_cat, fill=cat_col)

    # ── Divider ─────────────────────────────────────────────────────────────
    draw.rectangle([28, 80, W//2 + 80, 82], fill=(28, 30, 38))
    draw.rectangle([28, 80, 80, 82], fill=C_RED)

    # ── Title ───────────────────────────────────────────────────────────────
    TX, TY, TW = 36, 110, W - 100
    MAX_LINES = 3
    lines = _wrap_text(title, f_title, TW, draw)
    font_used = f_title
    if len(lines) > MAX_LINES:
        lines = _wrap_text(title, f_title_s, TW, draw)
        font_used = f_title_s
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        while lines[-1]:
            test = lines[-1] + "…"
            if draw.textbbox((0,0), test, font=font_used)[2] <= TW:
                lines[-1] = test; break
            lines[-1] = lines[-1][:-1].rstrip()

    lh = draw.textbbox((0,0), "Ag", font=font_used)[3] + 14
    for i, line in enumerate(lines):
        # Subtle red shadow for first line
        if i == 0:
            draw.text((TX+1, TY+1), line, font=font_used, fill=(40, 5, 5))
        draw.text((TX, TY + i*lh), line, font=font_used, fill=C_WHITE)

    # ── Bottom section ───────────────────────────────────────────────────────
    BY = H - 58
    draw.rectangle([28, BY-10, W-28, BY-9], fill=(22, 23, 30))

    # Source left
    draw.text((36, BY), source, font=f_src, fill=C_DIM)

    # Red dot separator
    draw.ellipse([36 + draw.textbbox((0,0), source, font=f_src)[2] + 10,
                  BY+7, 36 + draw.textbbox((0,0), source, font=f_src)[2] + 14,
                  BY+11], fill=C_RED)

    # Brand right
    brand = "@schweizintel"
    bw = draw.textbbox((0,0), brand, font=f_brand)[2]
    draw.text((W - bw - 28, BY), brand, font=f_brand, fill=C_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
