#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Svety Bot — логичный мастер создания открыток.

Зависимости (pip):
  python-telegram-bot==20.*
  pillow
  qrcode
  python-dotenv
Рекомендуется установить шрифт: fonts-dejavu-core (для DejaVuSans.ttf)

ENV (.env):
  TELEGRAM_BOT_TOKEN=7737841966:AAFIgmwHXNw1mvYZ8a4Jysl9KH1b_hb1x-c
  DOMAIN=svety.uz               # (опц.) для авто-URL в QR
  DATA_DIR=./data               # (опц.) где хранить проекты и превью
  MAX_UPLOAD_MB=10              # (опц.) лимит входящих изображений

Структура данных:
  data/<tg_id>/<project_id>/
    meta.json
    preview.jpg
    final.jpg
    bg.jpg (если пользователь загружал свой фон)

Запуск локально:
  python bot.py

Под systemd см. юнит-сервис из чата.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, Any

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import qrcode

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------
# Настройка логов и окружения
# -----------------------------
load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("svety-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("⚠️ TELEGRAM_BOT_TOKEN не задан (см. .env)")

DOMAIN = os.getenv("DOMAIN", "")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

# -------------
# Константы FSM
# -------------
MENU, CHOOSE_TPL, SET_BG, SET_TITLE, SET_SUBTITLE, SET_BODY, SET_STYLE, SET_QR, PREVIEW, PROJECTS = range(10)

# --------------------
# Утилиты и модели
# --------------------
@dataclass
class Project:
    id: str
    user_id: int
    template: str = "classic"
    bg_mode: str = "color"   # color|image|gradient
    bg_color: str = "#ffffff"
    bg_image: str | None = None
    title: str = ""
    subtitle: str = ""
    body: str = ""
    font_name: str = "DejaVuSans.ttf"
    font_color: str = "#111111"
    align: str = "center"      # left|center|right
    qr_enabled: bool = False
    qr_url: str = ""
    qr_pos: str = "br"         # tl|tr|bl|br|c
    qr_size: int = 220
    updated_at: float = field(default_factory=lambda: time.time())

    @property
    def root(self) -> Path:
        return DATA_DIR / str(self.user_id) / self.id

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Project":
        return cls(**d)


def new_project(user_id: int) -> Project:
    pid = uuid.uuid4().hex[:10]
    p = Project(id=pid, user_id=user_id)
    if DOMAIN:
        p.qr_url = f"https://{DOMAIN}/p/{pid}"  # будущая страница проекта на сайте
    return p


def save_project(p: Project) -> None:
    p.updated_at = time.time()
    p.root.mkdir(parents=True, exist_ok=True)
    meta = p.root / "meta.json"
    meta.write_text(json.dumps(p.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_project(user_id: int, pid: str) -> Project | None:
    meta = DATA_DIR / str(user_id) / pid / "meta.json"
    if not meta.exists():
        return None
    d = json.loads(meta.read_text(encoding="utf-8"))
    return Project.from_dict(d)


def list_projects(user_id: int, limit: int = 8) -> list[Project]:
    base = DATA_DIR / str(user_id)
    if not base.exists():
        return []
    items = []
    for child in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (child / "meta.json").exists():
            try:
                d = json.loads((child / "meta.json").read_text(encoding="utf-8"))
                items.append(Project.from_dict(d))
            except Exception:
                continue
        if len(items) >= limit:
            break
    return items


# ----------------------------
# Рендер открытки (Pillow)
# ----------------------------
CANVAS = (1200, 1500)  # 4:5 вертикаль, удобно для превью
MARGIN = 80


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.strip().lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c*2 for c in hex_color])
    if len(hex_color) != 6:
        return (255, 255, 255)
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b)


def get_font(font_name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Ищем шрифт в системе; если нет — падать нельзя
    candidates = [
        font_name,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_wrapped(draw: ImageDraw.ImageDraw, text: str, area: Tuple[int, int, int, int], font: ImageFont.ImageFont, fill: Tuple[int, int, int], align: str) -> None:
    if not text:
        return
    x0, y0, x1, y1 = area
    max_w = x1 - x0
    # примитивный перенос строк
    words = text.split()
    lines, line = [], []
    for w in words:
        t = " ".join(line + [w])
        wpx, _ = draw.textsize(t, font=font)
        if wpx <= max_w:
            line.append(w)
        else:
            if line:
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
        y += int(hpx * 1.25)


def render_image(p: Project, *, final: bool = False) -> Path:
    """Собрать превью/финал и вернуть путь к файлу."""
    W, H = CANVAS
    img = Image.new("RGB", CANVAS, color=hex_to_rgb(p.bg_color))
    draw = ImageDraw.Draw(img)

    # Фон-картинка
    if p.bg_mode == "image" and p.bg_image and Path(p.bg_image).exists():
        try:
            bg = Image.open(p.bg_image).convert("RGB")
            bg = bg.resize(CANVAS, Image.LANCZOS)
            img.paste(bg, (0, 0))
        except Exception as e:
            log.warning("Не удалось применить фоновое изображение: %s", e)

    # Заголовок / подзаголовок / текст
    title_font = get_font(p.font_name, 90)
    subtitle_font = get_font(p.font_name, 56)
    body_font = get_font(p.font_name, 44)
    color = hex_to_rgb(p.font_color)

    # Верхний блок — заголовок и подзаголовок
    top_area = (MARGIN, MARGIN, W - MARGIN, H // 2)
    draw_wrapped(draw, p.title, top_area, title_font, color, p.align)
    # немного ниже подзаголовок
    sub_area = (MARGIN, H // 2 - 80, W - MARGIN, H // 2 + 140)
    draw_wrapped(draw, p.subtitle, sub_area, subtitle_font, color, p.align)

    # Нижний блок — основной текст
    body_area = (MARGIN, H // 2 + 120, W - MARGIN, H - MARGIN - 260)
    draw_wrapped(draw, p.body, body_area, body_font, color, p.align)

    # QR
    if p.qr_enabled and p.qr_url:
        qr = qrcode.QRCode(border=1, box_size=10)
        qr.add_data(p.qr_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        # масштабируем к желаемому размеру
        qr_img = qr_img.resize((p.qr_size, p.qr_size), Image.LANCZOS)
        positions = {
            "tl": (MARGIN, MARGIN),
            "tr": (W - MARGIN - p.qr_size, MARGIN),
            "bl": (MARGIN, H - MARGIN - p.qr_size),
            "br": (W - MARGIN - p.qr_size, H - MARGIN - p.qr_size),
            "c": ((W - p.qr_size) // 2, (H - p.qr_size) // 2),
        }
        img.paste(qr_img, positions.get(p.qr_pos, positions["br"]))

    # Сохранение
    out = p.root / ("final.jpg" if final else "preview.jpg")
    img.save(out, quality=90)
    return out


# -------------------------------------
# UI: клавиатуры и парсер callback_data
# -------------------------------------

def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪄 Создать открытку", callback_data="a:new")],
        [InlineKeyboardButton("📁 Мои проекты", callback_data="a:list")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="a:settings")],
    ])


def kb_templates() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Classic", callback_data="a:tpl|id=classic"),
         InlineKeyboardButton("Minimal", callback_data="a:tpl|id=minimal")],
        [InlineKeyboardButton("Elegant", callback_data="a:tpl|id=elegant")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=menu"), InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_BG")]
    ]
    return InlineKeyboardMarkup(rows)


def kb_bg() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Фон: цвет", callback_data="a:bg|mode=color"),
         InlineKeyboardButton("Фон: фото", callback_data="a:bg|mode=image")],
        [InlineKeyboardButton("Цвет: белый", callback_data="a:bgcolor|c=#ffffff"),
         InlineKeyboardButton("Цвет: бежевый", callback_data="a:bgcolor|c=#fff7e6")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=tpl"),
         InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_TITLE")],
    ])


def kb_align_style() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Выровнять: влево", callback_data="a:align|v=left"),
         InlineKeyboardButton("по центру", callback_data="a:align|v=center"),
         InlineKeyboardButton("вправо", callback_data="a:align|v=right")],
        [InlineKeyboardButton("Цвет текста: тёмный", callback_data="a:fcolor|c=#111111"),
         InlineKeyboardButton("светлый", callback_data="a:fcolor|c=#ffffff")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=text"),
         InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_QR")],
    ])


def kb_qr(on: bool) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton("QR: Вкл" if not on else "QR: Выкл",
                                 callback_data=f"a:qr|enable={'1' if not on else '0'}")]
    row2 = [InlineKeyboardButton("Позиция ↖", callback_data="a:qrpos|p=tl"),
            InlineKeyboardButton("↗", callback_data="a:qrpos|p=tr"),
            InlineKeyboardButton("↙", callback_data="a:qrpos|p=bl"),
            InlineKeyboardButton("↘", callback_data="a:qrpos|p=br"),
            InlineKeyboardButton("● центр", callback_data="a:qrpos|p=c")]
    row3 = [InlineKeyboardButton("Размер −", callback_data="a:qrsize|d=-40"),
            InlineKeyboardButton("Размер +", callback_data="a:qrsize|d=40")]
    row4 = [InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=style"),
            InlineKeyboardButton("Предпросмотр", callback_data="a:to|s=PREVIEW")]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


def kb_preview(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Текст", callback_data="a:to|s=SET_TITLE"),
         InlineKeyboardButton("🖼 Фон", callback_data="a:to|s=SET_BG")],
        [InlineKeyboardButton("🅰️ Стиль", callback_data="a:to|s=SET_STYLE"),
         InlineKeyboardButton("🔗 QR", callback_data="a:to|s=SET_QR")],
        [InlineKeyboardButton("💾 Сохранить", callback_data=f"a:save|id={pid}")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="a:back|to=menu")],
    ])


def parse_cb(data: str) -> Tuple[str, Dict[str, str]]:
    # формат: a:<action>|k=v|k2=v2
    if not data.startswith("a:"):
        return data, {}
    data = data[2:]
    if "|" in data:
        action, rest = data.split("|", 1)
        kv = {}
        for part in rest.split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k] = v
        return action, kv
    return data, {}


# ---------------------
# Хелперы для контекста
# ---------------------

def get_current_project(context: ContextTypes.DEFAULT_TYPE) -> Project | None:
    pid = context.user_data.get("pid")
    uid = context._user_id  # type: ignore[attr-defined]
    if not pid:
        return None
    return load_project(uid, pid)


def set_current_project(context: ContextTypes.DEFAULT_TYPE, p: Project) -> None:
    context.user_data["pid"] = p.id
    save_project(p)


# ------------------
# Хэндлеры команд
# ------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Привет! Я помогу собрать красивую цифровую открытку ✨\n"
        "Нажми кнопку ниже, чтобы начать.",
        reply_markup=kb_menu(),
    )
    return MENU


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Главное меню:", reply_markup=kb_menu())
    return MENU


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Ок, отменил. Возвращайся, когда будешь готов 🙌")
    return ConversationHandler.END


# ------------------
# CallbackQuery flow
# ------------------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    action, kv = parse_cb(q.data or "")
    uid = update.effective_user.id

    if action == "new":
        p = new_project(uid)
        set_current_project(context, p)
        await q.edit_message_text(
            "Выбери шаблон:", reply_markup=kb_templates()
        )
        return CHOOSE_TPL

    if action == "list":
        items = list_projects(uid)
        if not items:
            await q.edit_message_text("У тебя пока нет проектов. Нажми \"Создать открытку\".", reply_markup=kb_menu())
            return MENU
        # Покажем списком названий и дат
        text = ["Твои последние проекты:"]
        for it in items:
            dt = datetime.fromtimestamp(it.updated_at).strftime("%Y-%m-%d %H:%M")
            text.append(f"• {it.id} — {dt}")
        text.append("\nНажми \"Создать открытку\" чтобы начать новый.")
        await q.edit_message_text("\n".join(text), reply_markup=kb_menu())
        return MENU

    if action == "tpl":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p.template = kv.get("id", p.template)
        save_project(p)
        await q.edit_message_text(
            "Фон. Можешь выбрать цвет или прислать фото (как изображение, не как файл).",
            reply_markup=kb_bg(),
        )
        return SET_BG

    if action == "bg":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        mode = kv.get("mode", "color")
        p.bg_mode = mode
        if mode == "color":
            p.bg_image = None
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_bg())
        return SET_BG

    if action == "bgcolor":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p.bg_color = kv.get("c", p.bg_color)
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_bg())
        return SET_BG

    if action == "align":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p.align = kv.get("v", p.align)
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_align_style())
        return SET_STYLE

    if action == "fcolor":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p.font_color = kv.get("c", p.font_color)
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_align_style())
        return SET_STYLE

    if action == "qr":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        enable = kv.get("enable") == "1"
        p.qr_enabled = enable
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_qr(p.qr_enabled))
        return SET_QR

    if action == "qrpos":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p.qr_pos = kv.get("p", p.qr_pos)
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_qr(p.qr_enabled))
        return SET_QR

    if action == "qrsize":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        try:
            delta = int(kv.get("d", "0"))
        except ValueError:
            delta = 0
        p.qr_size = max(120, min(400, p.qr_size + delta))
        save_project(p)
        await q.edit_message_reply_markup(reply_markup=kb_qr(p.qr_enabled))
        return SET_QR

    if action == "to":
        s = kv.get("s", "")
        if s == "SET_BG":
            await q.edit_message_text("Фон. Выбери цвет или пришли фото.", reply_markup=kb_bg())
            return SET_BG
        if s == "SET_TITLE":
            await q.edit_message_text("Напиши заголовок (одно сообщение). ✍️")
            return SET_TITLE
        if s == "SET_STYLE":
            await q.edit_message_text("Стиль текста:", reply_markup=kb_align_style())
            return SET_STYLE
        if s == "SET_QR":
            p = get_current_project(context)
            if not p:
                await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
                return MENU
            await q.edit_message_text(
                ("QR-код. Включи/выключи, позиция, размер.\n"
                 f"Текущий URL: {p.qr_url or 'не задан'}. Чтобы поменять URL — пришли ссылку текстом."),
                reply_markup=kb_qr(p.qr_enabled),
            )
            return SET_QR
        if s == "PREVIEW":
            p = get_current_project(context)
            if not p:
                await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
                return MENU
            path = await render_async(p, final=False)
            await q.edit_message_media(
                media=InputFile(path),
                reply_markup=kb_preview(p.id)
            )
            return PREVIEW
        return MENU

    if action == "back":
        to = kv.get("to", "menu")
        if to == "menu":
            await q.edit_message_text("Главное меню:", reply_markup=kb_menu())
            return MENU
        if to == "tpl":
            await q.edit_message_text("Выбери шаблон:", reply_markup=kb_templates())
            return CHOOSE_TPL
        if to == "style":
            await q.edit_message_text("Стиль текста:", reply_markup=kb_align_style())
            return SET_STYLE
        if to == "text":
            await q.edit_message_text("Напиши заголовок ✍️")
            return SET_TITLE
        return MENU

    if action == "save":
        p = get_current_project(context)
        if not p:
            await q.edit_message_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        path = await render_async(p, final=True)
        cap = "Готово! Это финальное изображение."
        if p.qr_url:
            cap += f"\nСсылка для QR: {p.qr_url}"
        await q.edit_message_caption(caption=cap) if q.message.photo else None
        await q.message.reply_photo(InputFile(path))
        await q.message.reply_text("Хочешь изменить что-то ещё?", reply_markup=kb_preview(p.id))
        return PREVIEW

    # по умолчанию
    await q.edit_message_text("Неизвестное действие. Вернёмся в меню.", reply_markup=kb_menu())
    return MENU


# ------------------------
# Обработка сообщений
# ------------------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = get_current_project(context)
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    if context.user_data.get("state") not in (SET_BG,):
        await update.effective_message.reply_text("Фото нужно на шаге *Фон*. Нажми \"Фон\".", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_preview(p.id) if context.user_data.get("state") == PREVIEW else None)
        return context.user_data.get("state", MENU)

    # Скачиваем
    photo = update.message.photo[-1]
    file = await photo.get_file()
    if photo.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await update.effective_message.reply_text(f"Файл слишком большой (>{MAX_UPLOAD_MB} МБ). Пришли поменьше.")
        return SET_BG
    p.root.mkdir(parents=True, exist_ok=True)
    out = p.root / "bg.jpg"
    await file.download_to_drive(out)
    p.bg_mode = "image"
    p.bg_image = str(out)
    save_project(p)
    await update.effective_message.reply_text("Фон обновлён. Можешь перейти к тексту.", reply_markup=kb_bg())
    return SET_BG


async def set_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = get_current_project(context)
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    p.title = (update.message.text or "").strip()
    save_project(p)
    await update.effective_message.reply_text("Отлично. Теперь пришли подзаголовок (если не нужен — пришли минус '-'):")
    return SET_SUBTITLE


async def set_subtitle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = get_current_project(context)
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    txt = (update.message.text or "").strip()
    p.subtitle = "" if txt == "-" else txt
    save_project(p)
    await update.effective_message.reply_text("Теперь основной текст (можно несколько предложений):")
    return SET_BODY


async def set_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = get_current_project(context)
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    p.body = (update.message.text or "").strip()
    save_project(p)
    # Покажем предпросмотр сразу
    path = await render_async(p, final=False)
    await update.effective_message.reply_photo(InputFile(path), caption="Предпросмотр. Можно настроить стиль, QR или сохранить.", reply_markup=kb_preview(p.id))
    return PREVIEW


async def set_qr_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    p = get_current_project(context)
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    url = (update.message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.effective_message.reply_text("Пришли полноценную ссылку, которая начинается с http:// или https://")
        return SET_QR
    p.qr_url = url
    p.qr_enabled = True
    save_project(p)
    await update.effective_message.reply_text("URL для QR сохранён.", reply_markup=kb_qr(True))
    return SET_QR


# -------------------------------
# Рендер асинхронно (не блочим)
# -------------------------------
async def render_async(p: Project, *, final: bool) -> Path:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, render_image, p, final)


# ------------------
# Ошибки и fallback
# ------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Exception in handler: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Упс, что-то пошло не так. Я уже записал это в логи и скоро починю.")
    except Exception:
        pass


# ------------------
# Main() / запуск
# ------------------

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("menu", cmd_menu),
        ],
        states={
            MENU: [CallbackQueryHandler(on_cb)],
            CHOOSE_TPL: [CallbackQueryHandler(on_cb)],
            SET_BG: [CallbackQueryHandler(on_cb), MessageHandler(filters.PHOTO, on_photo)],
            SET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_title)],
            SET_SUBTITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_subtitle)],
            SET_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_body)],
            SET_STYLE: [CallbackQueryHandler(on_cb)],
            SET_QR: [CallbackQueryHandler(on_cb), MessageHandler(filters.TEXT & ~filters.COMMAND, set_qr_text)],
            PREVIEW: [CallbackQueryHandler(on_cb)],
            PROJECTS: [CallbackQueryHandler(on_cb)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(on_error)
    return app


if __name__ == "__main__":
    log.info("Starting Svety Bot…")
    application = build_app()
    application.run_polling(allowed_updates=["message", "callback_query"])
