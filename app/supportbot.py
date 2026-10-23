"""Бот поддержки (один на сервер): владельцы, мастера и клиенты всех салонов пишут сюда,
сообщения приходят разработчику, он отвечает свайпом («Ответить») — ответ уходит человеку.

Кнопка «🆘 Поддержка» в ботах салонов ведёт на t.me/<бот>?start=<салон>-<роль>,
поэтому видно, из какого салона и кто пишет. Работает внутри менеджера, как бот техподдержки.
"""
import asyncio
import html
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, ReactionTypeEmoji

from . import config
from .config import LOG_OWNER, SHARED_DIR

log = logging.getLogger("supportbot")
router = Router()
BASE = Path(__file__).resolve().parent.parent
TENANTS = BASE / "tenants"
ROLE_TITLES = {"owner": "👑 владелец", "master": "✂️ мастер", "client": "👤 клиент"}
INFO_FILE = "support.json"  # {"username": "..."} — салоны берут отсюда адрес бота для кнопки


# ---------- хранилище: кто откуда пишет и какое сообщение у оператора чьё ----------
def _db() -> sqlite3.Connection:
    SHARED_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(SHARED_DIR / "support.db", timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, slug TEXT DEFAULT '', role TEXT DEFAULT '',
                                          name TEXT DEFAULT '', username TEXT DEFAULT '', last_at REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS map (op_msg INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
    """)
    return c


def _operator_id() -> int:
    """Кому приходят обращения: разработчик (тот же, что в боте техподдержки)."""
    for name in ("admin.json", INFO_FILE):
        try:
            v = json.loads((SHARED_DIR / name).read_text(encoding="utf-8")).get("dev_id" if name == "admin.json" else "operator")
            if v:
                return int(v)
        except Exception:
            pass
    return 0


def _save_info(**kv):
    f = SHARED_DIR / INFO_FILE
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.update(kv)
    SHARED_DIR.mkdir(exist_ok=True)
    f.write_text(json.dumps(d), encoding="utf-8")


def _is_operator(u) -> bool:
    op = _operator_id()
    return u.id == op if op else (u.username or "").lower() == LOG_OWNER


def _salon_name(slug: str) -> str:
    f = TENANTS / slug / "tenant.env"
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("NAME="):
                return line[5:].strip().strip('"')
    except Exception:
        pass
    return slug


def _who(row) -> str:
    parts = []
    if row["slug"]:
        parts.append(f"«{html.escape(_salon_name(row['slug']))}»")
    if row["role"]:
        parts.append(ROLE_TITLES.get(row["role"], row["role"]))
    name = html.escape(row["name"] or "без имени")
    parts.append(name + (f" @{row['username']}" if row["username"] else ""))
    return " · ".join(parts)


# ---------- пользователи ----------
@router.message(CommandStart())
async def start(message: Message, command: CommandObject):
    u = message.from_user
    if _is_operator(u):
        _save_info(operator=u.id)
        await message.answer("🛠 Вы — оператор поддержки. Сюда будут приходить обращения из всех салонов.\n\n"
                             "Чтобы ответить — свайпните сообщение («Ответить») и напишите текст, можно с фото.")
        return
    slug, role = "", ""
    m = re.fullmatch(r"([a-z0-9_]+)-(owner|master|client)", command.args or "")
    if m and (TENANTS / m.group(1)).is_dir():
        slug, role = m.group(1), m.group(2)
    with _db() as c:
        old = c.execute("SELECT slug, role FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not slug and old:  # открыл бота без кнопки — помним, откуда писал раньше
            slug, role = old["slug"], old["role"]
        c.execute("INSERT OR REPLACE INTO users (user_id, slug, role, name, username, last_at) VALUES (?,?,?,?,?,?)",
                  (u.id, slug, role, u.full_name, u.username or "", 0))
    where = f" ({html.escape(_salon_name(slug))})" if slug else ""
    await message.answer(
        f"👋 Здравствуйте! Это поддержка онлайн-записи{where}.\n\n"
        "Опишите, что случилось или что непонятно, — можно приложить скриншот. "
        "Отвечу здесь же, обычно в течение дня.")


@router.message(F.chat.type == "private")
async def incoming(message: Message):
    u = message.from_user
    if _is_operator(u):
        await operator_reply(message)
        return
    op = _operator_id()
    if not op:
        await message.answer("Поддержка ещё не настроена. Попробуйте позже 🙏")
        log.warning("Бот поддержки: обращение есть, а оператора нет — откройте бота поддержки со своего аккаунта")
        return
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row:
            c.execute("INSERT INTO users (user_id, name, username) VALUES (?,?,?)", (u.id, u.full_name, u.username or ""))
            row = c.execute("SELECT * FROM users WHERE user_id=?", (u.id,)).fetchone()
        first = time.time() - (row["last_at"] or 0) > 3600  # подтверждение — не на каждое сообщение
        c.execute("UPDATE users SET name=?, username=?, last_at=? WHERE user_id=?",
                  (u.full_name, u.username or "", time.time(), u.id))
    try:
        head = await message.bot.send_message(op, f"🆘 {_who(row)}")
        copy = await message.bot.copy_message(op, message.chat.id, message.message_id, reply_to_message_id=head.message_id)
    except Exception as e:
        log.warning("Бот поддержки: не удалось переслать обращение: %s", e)
        await message.answer("Не получилось отправить, попробуйте ещё раз через минуту 🙏")
        return
    with _db() as c:
        c.executemany("INSERT OR REPLACE INTO map (op_msg, user_id) VALUES (?,?)",
                      [(head.message_id, u.id), (copy.message_id, u.id)])
    if first:
        await message.answer("✅ Получил, отвечу здесь же.")


async def operator_reply(message: Message):
    r = message.reply_to_message
    if not r:
        await message.answer("Чтобы ответить человеку, свайпните его сообщение («Ответить») и напишите текст.")
        return
    with _db() as c:
        row = c.execute("SELECT user_id FROM map WHERE op_msg=?", (r.message_id,)).fetchone()
    if not row:
        await message.answer("Не понял, кому это. Ответьте на сообщение с 🆘 или на само обращение.")
        return
    try:
        await message.bot.copy_message(row["user_id"], message.chat.id, message.message_id)
    except Exception as e:
        await message.answer(f"❌ Не доставлено (человек мог остановить бота): {html.escape(str(e))[:200]}")
        return
    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])  # отметка «доставлено»
    except Exception:
        await message.answer("✅ Отправлено")


async def run():
    import os
    token = config.SUPPORT_BOT_TOKEN or os.getenv("SUPPORT_BOT_TOKEN", "").strip()
    if not token:
        return
    while True:
        try:
            bot = Bot(token, session=config.tg_session(), default=DefaultBotProperties(parse_mode="HTML"))
            me = await bot.get_me()
            _save_info(username=me.username)
            log.info("Бот поддержки: https://t.me/%s", me.username)
            dp = Dispatcher()
            dp.include_router(router)
            await dp.start_polling(bot, handle_signals=False, polling_timeout=config.POLL_TIMEOUT)
            return
        except Exception as e:
            log.warning("Бот поддержки не запустился (проверьте SUPPORT_BOT_TOKEN): %s — повтор через минуту", e)
            await asyncio.sleep(60)
