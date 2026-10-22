"""Бот техподдержки (один на весь сервер).

Позволяет владельцу проекта (username = LOG_OWNER) войти в ЛЮБОЙ салон в роли
клиента, хозяина или мастера. Режим пишется в settings того салона (ключ dev_mode),
а личность разработчика — в shared/admin.json (dev_id). Салон показывает владельцу
в кабинете строку «Техподдержка подключена», так что доступ не скрытый.

Запускается в менеджере (manager.py). Своего токена нет — берётся ADMIN_BOT_TOKEN.
"""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import config
from .config import LOG_OWNER, SHARED_DIR

log = logging.getLogger("adminbot")
router = Router()

BASE = Path(__file__).resolve().parent.parent
TENANTS = BASE / "tenants"
ROLES = {"client": "👤 Клиент", "owner": "👑 Хозяин", "master": "✂️ Мастер"}


def _allowed(message_or_cb) -> bool:
    return (message_or_cb.from_user.username or "").lower() == LOG_OWNER


def set_dev_id(tg_id: int):
    SHARED_DIR.mkdir(exist_ok=True)
    (SHARED_DIR / "admin.json").write_text(json.dumps({"dev_id": tg_id}), encoding="utf-8")


def _tenant_cfg(slug: str) -> dict:
    data = {}
    f = TENANTS / slug / "tenant.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"')
    return data


def _salons() -> list[dict]:
    out = []
    if not TENANTS.exists():
        return out
    for d in sorted(TENANTS.iterdir()):
        if not (d.is_dir() and (d / "tenant.env").exists()):
            continue
        cfg = _tenant_cfg(d.name)
        bot = ""
        try:
            bot = json.loads((d / "status.json").read_text(encoding="utf-8")).get("bot") or ""
        except Exception:
            pass
        out.append({"slug": d.name, "name": cfg.get("NAME", d.name),
                    "db": d / (cfg.get("DB_FILE") or "lunka.db"), "bot": bot})
    return out


def _salon(slug: str) -> dict | None:
    return next((s for s in _salons() if s["slug"] == slug), None)


def _db(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _set_mode(db_path: Path, mode: str):
    with _db(db_path) as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dev_mode', ?)", (mode,))
        c.commit()


def _masters(db_path: Path) -> list[dict]:
    try:
        with _db(db_path) as c:
            return [dict(r) for r in c.execute("SELECT id, name FROM masters WHERE active=1 ORDER BY id")]
    except Exception as e:
        log.warning("masters %s: %s", db_path, e)
        return []


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _show_salons(target):
    salons = _salons()
    if not salons:
        await target.answer("Пока нет ни одного салона на сервере.")
        return
    rows = [[_btn(s["name"], f"dv:s:{s['slug']}")] for s in salons]
    text = "🛠 <b>Техподдержка</b>\n\nВыберите салон, в который хотите войти:"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=_kb(rows))
    else:
        await target.message.edit_text(text, reply_markup=_kb(rows))


@router.message(CommandStart())
async def start(message: Message):
    if not _allowed(message):
        await message.answer("Это служебный бот проекта. Доступ закрыт.")
        return
    set_dev_id(message.from_user.id)
    await _show_salons(message)


@router.message(Command("stop"))
async def stop(message: Message):
    if not _allowed(message):
        return
    for s in _salons():
        try:
            _set_mode(s["db"], "")
        except Exception:
            pass
    await message.answer("Режим техподдержки выключен во всех салонах. Вы снова обычный пользователь.")


@router.callback_query(F.data == "dv:home")
async def back_home(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    await cb.answer()
    await _show_salons(cb)


@router.callback_query(F.data.startswith("dv:s:"))
async def pick_salon(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    slug = cb.data.split(":", 2)[2]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    rows = [[_btn(ROLES["client"], f"dv:r:{slug}:client")],
            [_btn(ROLES["owner"], f"dv:r:{slug}:owner")],
            [_btn(ROLES["master"], f"dv:m:{slug}")],
            [_btn("◀️ Назад", "dv:home")]]
    await cb.message.edit_text(f"Салон «{s['name']}». В какой роли войти?", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("dv:m:"))
async def pick_master_list(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    slug = cb.data.split(":", 2)[2]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    ms = _masters(s["db"])
    if not ms:
        await cb.message.edit_text("В этом салоне пока нет мастеров.",
                                   reply_markup=_kb([[_btn("◀️ Назад", f"dv:s:{slug}")]]))
        return
    rows = [[_btn(m["name"], f"dv:r:{slug}:master:{m['id']}")] for m in ms]
    rows.append([_btn("◀️ Назад", f"dv:s:{slug}")])
    await cb.message.edit_text(f"Салон «{s['name']}». Войти как мастер:", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("dv:r:"))
async def apply_role(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    parts = cb.data.split(":")  # dv:r:<slug>:<role>[:<mid>]
    slug, role = parts[2], parts[3]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    mode = f"master:{parts[4]}" if role == "master" else role
    try:
        _set_mode(s["db"], mode)
    except Exception as e:
        await cb.answer(f"Не удалось: {e}", show_alert=True)
        return
    set_dev_id(cb.from_user.id)
    await cb.answer("Готово")
    label = ROLES.get(role, role)
    if s["bot"]:
        link = f"https://t.me/{s['bot']}?start=dev"
        open_row = [[InlineKeyboardButton(text=f"Открыть «{s['name']}»", url=link)]]
    else:
        open_row = []
    await cb.message.edit_text(
        f"✅ В салоне «{s['name']}» вы теперь входите как <b>{label}</b>.\n\n"
        f"Откройте бота салона и нажмите «Запустить» (/start) — увидите его меню в этой роли. "
        f"Чтобы вернуться в обычный режим — /stop.",
        reply_markup=_kb(open_row + [[_btn("◀️ К списку салонов", "dv:home")]]))


async def run():
    token = config.ADMIN_BOT_TOKEN
    if not token:
        return
    try:
        bot = Bot(token, session=config.tg_session(), default=DefaultBotProperties(parse_mode="HTML"))
        me = await bot.get_me()
        log.info("Бот техподдержки: https://t.me/%s (доступ: @%s)", me.username, LOG_OWNER)
        dp = Dispatcher()
        dp.include_router(router)
        await dp.start_polling(bot, handle_signals=False, polling_timeout=config.POLL_TIMEOUT)
    except Exception as e:
        log.warning("Бот техподдержки не запустился (проверьте ADMIN_BOT_TOKEN): %s", e)
