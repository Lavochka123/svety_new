# svety/core/rendering.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Tuple

from PIL import Image, ImageDraw, ImageFont
import qrcode

from .config import cfg

log = logging.getLogger(__name__)

# Полотно и отступы (вертикальный формат 4:5 удобно для превью/мобилы)
CANVAS: Tuple[int, int] = (1200, 1500)
MARGIN = 80


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    s = (hex_color or "#ffffff").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return r, g, b
    except Exception:
        return 255, 255, 255


def _get_font(font_name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Пытаемся открыть указанный шрифт; если не получилось — падаем на системные DejaVu/FreeSans,
    иначе — на встроенный bitmap-шрифт (нежелательно, но безопасно).
    """
    candidates = [
        font_name,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        try:
            if p:
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    area: Tuple[int, int, int, int],
    font: ImageFont.ImageFont,
    fill: Tuple[int, int, int],
    align: str = "center",
    line_height: float = 1.25,
) -> None:
    """
    Примитивный перенос слов в заданный прямоугольник.
    """
    if not text:
        return
    x0, y0, x1, y1 = area
    max_w = x1 - x0
    words = text.split()
    lines, line = [], []
    for w in words:
        probe = (" ".join(line + [w])).strip()
        wpx, _ = draw.textsize(probe, font=font)
        if wpx <= max_w or not line:
            line.append(w)
        else:
            lines.append(" ".join(line))
            line = [w]
    if line:
        lines.append(" ".join(line))

    y = y0
    for ln in lines:
        wpx, hpx = draw.textsize(ln, font=font)
        if align == "left":
            x = x0
        elif align == "right":
            x = x1 - wpx
        else:
            x = x0 + (max_w - wpx) // 2
        if y + hpx > y1:
            break
        draw.text((x, y), ln, font=font, fill=fill)
        y += int(hpx * line_height)


def _project_dir(p: Any) -> Path:
    """
    Каталог проекта на диске: DATA_DIR/<tg_id>/<project_id>
    Требуются атрибуты p.user_id и p.id
    """
    base = cfg.DATA_DIR / str(getattr(p, "user_id", "unknown")) / str(getattr(p, "id", "unknown"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def render_image(p: Any, *, final: bool = False) -> Path:
    """
    Сборка изображения (превью или финал) по параметрам проекта `p`.
    Ожидаемые атрибуты у `p` (если чего-то нет — применим дефолты):
      - template: str (не используется напрямую, на будущее)
      - bg_mode: 'color'|'image', bg_color: '#rrggbb', bg_image: str|None
      - title, subtitle, body: str
      - font_name: путь к ttf|ttc, font_color: '#rrggbb', align: 'left'|'center'|'right'
      - qr_enabled: bool, qr_url: str, qr_pos: 'tl'|'tr'|'bl'|'br'|'c', qr_size: int
      - id: str, user_id: int  (для путей)
    """
    W, H = CANVAS
    bg_color = _hex_to_rgb(getattr(p, "bg_color", "#ffffff"))
    img = Image.new("RGB", CANVAS, color=bg_color)
    draw = ImageDraw.Draw(img)

    # Фон-картинка
    bg_mode = getattr(p, "bg_mode", "color")
    bg_image = getattr(p, "bg_image", None)
    if bg_mode == "image" and bg_image:
        try:
            bg_path = Path(str(bg_image))
            if bg_path.exists():
                bg = Image.open(bg_path).convert("RGB").resize(CANVAS, Image.LANCZOS)
                img.paste(bg, (0, 0))
        except Exception as e:
            log.warning("Не удалось применить фоновое изображение: %s", e)

    # Тексты
    font_name = getattr(p, "font_name", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    color = _hex_to_rgb(getattr(p, "font_color", "#111111"))
    align = getattr(p, "align", "center")

    title = getattr(p, "title", "")
    subtitle = getattr(p, "subtitle", "")
    body = getattr(p, "body", "")

    title_font = _get_font(font_name, 90)
    subtitle_font = _get_font(font_name, 56)
    body_font = _get_font(font_name, 44)

    # Области
    top_area = (MARGIN, MARGIN, W - MARGIN, H // 2)
    sub_area = (MARGIN, H // 2 - 80, W - MARGIN, H // 2 + 140)
    body_area = (MARGIN, H // 2 + 120, W - MARGIN, H - MARGIN - 260)

    _text_block(draw, title, top_area, title_font, color, align)
    _text_block(draw, subtitle, sub_area, subtitle_font, color, align)
    _text_block(draw, body, body_area, body_font, color, align)

    # QR-код
    if getattr(p, "qr_enabled", False) and getattr(p, "qr_url", ""):
        try:
            size = max(120, min(600, int(getattr(p, "qr_size", 220))))
        except Exception:
            size = 220
        qr = qrcode.QRCode(border=1, box_size=10)
        qr.add_data(getattr(p, "qr_url"))
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_img = qr_img.resize((size, size), Image.LANCZOS)

        positions = {
            "tl": (MARGIN, MARGIN),
            "tr": (W - MARGIN - size, MARGIN),
            "bl": (MARGIN, H - MARGIN - size),
            "br": (W - MARGIN - size, H - MARGIN - size),
            "c": ((W - size) // 2, (H - size) // 2),
        }
        pos_key = getattr(p, "qr_pos", "br")
        img.paste(qr_img, positions.get(pos_key, positions["br"]))

    # Сохранение
    out_dir = _project_dir(p)
    out = out_dir / ("final.jpg" if final else "preview.jpg")
    try:
        img.save(out, quality=90)
    except Exception as e:
        log.error("Не удалось сохранить изображение: %s", e)
        raise
    return out
