# svety/bot/main.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Tuple, Any, List

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

from svety.core.config import cfg
from svety.core.rendering import render_image

# -----------------------------
# ЛОГИ И ТОКЕН
# -----------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("svety-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("⚠️ TELEGRAM_BOT_TOKEN не задан в .env")

# -----------------------------
# СОСТОЯНИЯ ДИАЛОГА
# -----------------------------
MENU, CHOOSE_TPL, SET_BG, SET_TITLE, SET_SUBTITLE, SET_BODY, SET_STYLE, SET_QR, PREVIEW, PROJECTS = range(10)

# -----------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -----------------------------
def _user_dir(user_id: int) -> Path:
    p = cfg.DATA_DIR / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _project_dir(user_id: int, pid: str) -> Path:
    p = _user_dir(user_id) / pid
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_path(user_id: int, pid: str) -> Path:
    return _project_dir(user_id, pid) / "meta.json"


def new_project(user_id: int) -> Dict[str, Any]:
    pid = uuid.uuid4().hex[:10]
    proj = {
        "id": pid,
        "user_id": user_id,
        "template": "classic",
        "bg_mode": "color",
        "bg_color": "#ffffff",
        "bg_image": None,
        "title": "",
        "subtitle": "",
        "body": "",
        "font_name": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "font_color": "#111111",
        "align": "center",
        "qr_enabled": False,
        "qr_url": f"https://{cfg.DOMAIN}/p/{pid}" if cfg.DOMAIN else "",
        "qr_pos": "br",
        "qr_size": 220,
        "updated_at": time.time(),
    }
    save_project(proj)
    return proj


def save_project(p: Dict[str, Any]) -> None:
    p["updated_at"] = time.time()
    meta = _meta_path(p["user_id"], p["id"])
    meta.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def load_project(user_id: int, pid: str) -> Dict[str, Any] | None:
    meta = _meta_path(user_id, pid)
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_projects(user_id: int, limit: int = 8) -> List[Dict[str, Any]]:
    base = _user_dir(user_id)
    items: List[Dict[str, Any]] = []
    if not base.exists():
        return items
    for child in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        m = child / "meta.json"
        if m.exists():
            try:
                d = json.loads(m.read_text(encoding="utf-8"))
                items.append(d)
            except Exception:
                continue
        if len(items) >= limit:
            break
    return items


def as_obj(d: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**d)


# -----------------------------
# КЛАВИАТУРЫ
# -----------------------------
def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🪄 Создать открытку", callback_data="a:new")],
            [InlineKeyboardButton("📁 Мои проекты", callback_data="a:list")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="a:settings")],
        ]
    )


def kb_templates() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Classic", callback_data="a:tpl|id=classic"),
                InlineKeyboardButton("Minimal", callback_data="a:tpl|id=minimal"),
            ],
            [InlineKeyboardButton("Elegant", callback_data="a:tpl|id=elegant")],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=menu"),
                InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_BG"),
            ],
        ]
    )


def kb_bg() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Фон: цвет", callback_data="a:bg|mode=color"),
                InlineKeyboardButton("Фон: фото", callback_data="a:bg|mode=image"),
            ],
            [
                InlineKeyboardButton("Цвет: белый", callback_data="a:bgcolor|c=#ffffff"),
                InlineKeyboardButton("бежевый", callback_data="a:bgcolor|c=#fff7e6"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=tpl"),
                InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_TITLE"),
            ],
        ]
    )


def kb_align_style() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Влево", callback_data="a:align|v=left"),
                InlineKeyboardButton("По центру", callback_data="a:align|v=center"),
                InlineKeyboardButton("Вправо", callback_data="a:align|v=right"),
            ],
            [
                InlineKeyboardButton("Текст: тёмный", callback_data="a:fcolor|c=#111111"),
                InlineKeyboardButton("светлый", callback_data="a:fcolor|c=#ffffff"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=text"),
                InlineKeyboardButton("Далее ➡️", callback_data="a:to|s=SET_QR"),
            ],
        ]
    )


def kb_qr(on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "QR: Вкл" if not on else "QR: Выкл",
                    callback_data=f"a:qr|enable={'1' if not on else '0'}",
                )
            ],
            [
                InlineKeyboardButton("↖", callback_data="a:qrpos|p=tl"),
                InlineKeyboardButton("↗", callback_data="a:qrpos|p=tr"),
                InlineKeyboardButton("↙", callback_data="a:qrpos|p=bl"),
                InlineKeyboardButton("↘", callback_data="a:qrpos|p=br"),
                InlineKeyboardButton("● центр", callback_data="a:qrpos|p=c"),
            ],
            [
                InlineKeyboardButton("Размер −", callback_data="a:qrsize|d=-40"),
                InlineKeyboardButton("Размер +", callback_data="a:qrsize|d=40"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="a:back|to=style"),
                InlineKeyboardButton("Предпросмотр", callback_data="a:to|s=PREVIEW"),
            ],
        ]
    )


def kb_preview(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Текст", callback_data="a:to|s=SET_TITLE"),
                InlineKeyboardButton("🖼 Фон", callback_data="a:to|s=SET_BG"),
            ],
            [
                InlineKeyboardButton("🅰️ Стиль", callback_data="a:to|s=SET_STYLE"),
                InlineKeyboardButton("🔗 QR", callback_data="a:to|s=SET_QR"),
            ],
            [InlineKeyboardButton("💾 Сохранить", callback_data=f"a:save|id={pid}")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="a:back|to=menu")],
        ]
    )


def parse_cb(data: str) -> Tuple[str, Dict[str, str]]:
    """
    Формат callback_data: a:<action>|k=v|k2=v2
    """
    if not data or not data.startswith("a:"):
        return data or "", {}
    payload = data[2:]
    if "|" not in payload:
        return payload, {}
    action, rest = payload.split("|", 1)
    kv: Dict[str, str] = {}
    for part in rest.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k] = v
    return action, kv


# -----------------------------
# ХЭНДЛЕРЫ КОМАНД
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Привет! Я помогу собрать красивую цифровую открытку ✨\nНажми кнопку ниже, чтобы начать.",
        reply_markup=kb_menu(),
    )
    return MENU


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Главное меню:", reply_markup=kb_menu())
    return MENU


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Ок, отменил. Возвращайся, когда будешь готов 🙌")
    return ConversationHandler.END


# -----------------------------
# CALLBACK FLOW
# -----------------------------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    action, kv = parse_cb(q.data or "")
    uid = update.effective_user.id

    # вспом. функция получения/сохранения текущего проекта
    def get_proj() -> Dict[str, Any] | None:
        pid = context.user_data.get("pid")
        if not pid:
            return None
        return load_project(uid, pid)

    if action == "new":
        p = new_project(uid)
        context.user_data["pid"] = p["id"]
        await q.message.reply_text("Выбери шаблон:", reply_markup=kb_templates())
        return CHOOSE_TPL

    if action == "list":
        items = list_projects(uid)
        if not items:
            await q.message.reply_text("У тебя пока нет проектов. Нажми «Создать открытку».", reply_markup=kb_menu())
            return MENU
        lines = ["Твои последние проекты:"]
        for it in items:
            dt = datetime.fromtimestamp(it.get("updated_at", time.time())).strftime("%Y-%m-%d %H:%M")
            lines.append(f"• {it.get('id')} — {dt}")
        await q.message.reply_text("\n".join(lines), reply_markup=kb_menu())
        return MENU

    if action == "tpl":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p["template"] = kv.get("id", p["template"])
        save_project(p)
        await q.message.reply_text("Фон. Выбери цвет или пришли фото (как изображение, не как файл).", reply_markup=kb_bg())
        return SET_BG

    if action == "bg":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        mode = kv.get("mode", "color")
        p["bg_mode"] = mode
        if mode == "color":
            p["bg_image"] = None
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_bg())
        return SET_BG

    if action == "bgcolor":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p["bg_color"] = kv.get("c", p["bg_color"])
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_bg())
        return SET_BG

    if action == "align":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p["align"] = kv.get("v", p["align"])
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_align_style())
        return SET_STYLE

    if action == "fcolor":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p["font_color"] = kv.get("c", p["font_color"])
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_align_style())
        return SET_STYLE

    if action == "qr":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        enable = kv.get("enable") == "1"
        p["qr_enabled"] = enable
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_qr(p["qr_enabled"]))
        return SET_QR

    if action == "qrpos":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        p["qr_pos"] = kv.get("p", p["qr_pos"])
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_qr(p["qr_enabled"]))
        return SET_QR

    if action == "qrsize":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        try:
            delta = int(kv.get("d", "0"))
        except ValueError:
            delta = 0
        p["qr_size"] = max(120, min(400, p["qr_size"] + delta))
        save_project(p)
        await q.message.reply_markup(reply_markup=kb_qr(p["qr_enabled"]))
        return SET_QR

    if action == "to":
        s = kv.get("s", "")
        if s == "SET_BG":
            await q.message.reply_text("Фон. Выбери цвет или пришли фото.", reply_markup=kb_bg())
            return SET_BG
        if s == "SET_TITLE":
            await q.message.reply_text("Напиши заголовок (одно сообщение). ✍️")
            return SET_TITLE
        if s == "SET_STYLE":
            await q.message.reply_text("Стиль текста:", reply_markup=kb_align_style())
            return SET_STYLE
        if s == "SET_QR":
            p = get_proj()
            if not p:
                await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
                return MENU
            await q.message.reply_text(
                ("QR-код. Включи/выключи, позиция, размер.\n"
                 f"Текущий URL: {p.get('qr_url') or 'не задан'}. Чтобы поменять URL — пришли ссылку текстом."),
                reply_markup=kb_qr(p["qr_enabled"]),
            )
            return SET_QR
        if s == "PREVIEW":
            p = get_proj()
            if not p:
                await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
                return MENU
            path = await render_async(as_obj(p), final=False)
            await q.message.reply_photo(InputFile(path), caption="Предпросмотр. Можно настроить стиль, QR или сохранить.", reply_markup=kb_preview(p["id"]))
            return PREVIEW
        return MENU

    if action == "back":
        to = kv.get("to", "menu")
        if to == "menu":
            await q.message.reply_text("Главное меню:", reply_markup=kb_menu())
            return MENU
        if to == "tpl":
            await q.message.reply_text("Выбери шаблон:", reply_markup=kb_templates())
            return CHOOSE_TPL
        if to == "style":
            await q.message.reply_text("Стиль текста:", reply_markup=kb_align_style())
            return SET_STYLE
        if to == "text":
            await q.message.reply_text("Напиши заголовок ✍️")
            return SET_TITLE
        return MENU

    if action == "save":
        p = get_proj()
        if not p:
            await q.message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
            return MENU
        path = await render_async(as_obj(p), final=True)
        cap = "Готово! Это финальное изображение."
        if p.get("qr_url"):
            cap += f"\nСсылка для QR: {p['qr_url']}"
        await q.message.reply_photo(InputFile(path), caption=cap, reply_markup=kb_preview(p["id"]))
        return PREVIEW

    # неизвестное действие
    await q.message.reply_text("Неизвестное действие. Вернёмся в меню.", reply_markup=kb_menu())
    return MENU


# -----------------------------
# ОБРАБОТКА СООБЩЕНИЙ
# -----------------------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    pid = context.user_data.get("pid")
    if not pid:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    p = load_project(uid, pid)
    if not p:
        await update.effective_message.reply_text("Проект не найден. Начнём заново?", reply_markup=kb_menu())
        return MENU

    # Принимаем фото только на шаге выбора фона
    state = context.user_data.get("state")
    if state not in (SET_BG, None):
        await update.effective_message.reply_text(
            "Фото нужно на шаге *Фон*. Нажми «Фон».",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_preview(p["id"]),
        )
        return state or MENU

    photo = update.message.photo[-1]
    tgfile = await photo.get_file()
    if photo.file_size and photo.file_size > cfg.MAX_UPLOAD_MB * 1024 * 1024:
        await update.effective_message.reply_text(f"Файл слишком большой (>{cfg.MAX_UPLOAD_MB} МБ). Пришли поменьше.")
        return SET_BG

    out = _project_dir(uid, pid) / "bg.jpg"
    await tgfile.download_to_drive(out)

    p["bg_mode"] = "image"
    p["bg_image"] = str(out)
    save_project(p)

    await update.effective_message.reply_text("Фон обновлён. Можешь перейти к тексту.", reply_markup=kb_bg())
    return SET_BG


async def set_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    pid = context.user_data.get("pid")
    if not pid:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU
    p = load_project(uid, pid)
    if not p:
        await update.effective_message.reply_text("Проект не найден. Начнём заново?", reply_markup=kb_menu())
        return MENU

    p["title"] = (update.message.text or "").strip()
    save_project(p)
    await update.effective_message.reply_text("Отлично. Теперь пришли подзаголовок (если не нужен — пришли минус '-'):")
    return SET_SUBTITLE


async def set_subtitle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    pid = context.user_data.get("pid")
    p = load_project(uid, pid) if pid else None
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU

    txt = (update.message.text or "").strip()
    p["subtitle"] = "" if txt == "-" else txt
    save_project(p)
    await update.effective_message.reply_text("Теперь основной текст (можно несколько предложений):")
    return SET_BODY


async def set_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    pid = context.user_data.get("pid")
    p = load_project(uid, pid) if pid else None
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU

    p["body"] = (update.message.text or "").strip()
    save_project(p)

    # Покажем предпросмотр
    path = await render_async(as_obj(p), final=False)
    await update.effective_message.reply_photo(
        InputFile(path),
        caption="Предпросмотр. Можно настроить стиль, QR или сохранить.",
        reply_markup=kb_preview(p["id"]),
    )
    return PREVIEW


async def set_qr_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    pid = context.user_data.get("pid")
    p = load_project(uid, pid) if pid else None
    if not p:
        await update.effective_message.reply_text("Сначала начнём новый проект.", reply_markup=kb_menu())
        return MENU

    url = (update.message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.effective_message.reply_text("Пришли полноценную ссылку, которая начинается с http:// или https://")
        return SET_QR
    p["qr_url"] = url
    p["qr_enabled"] = True
    save_project(p)
    await update.effective_message.reply_text("URL для QR сохранён.", reply_markup=kb_qr(True))
    return SET_QR


# -----------------------------
# РЕНДЕР В EXECUTOR (НЕ БЛОЧИМ)
# -----------------------------
async def render_async(p_obj: Any, *, final: bool) -> Path:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, render_image, p_obj, final)


# -----------------------------
# ОШИБКИ
# -----------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Exception in handler: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Упс, что-то пошло не так. Я уже записал это в логи и скоро починю.")
    except Exception:
        pass


# -----------------------------
# СБОРКА ПРИЛОЖЕНИЯ
# -----------------------------
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
