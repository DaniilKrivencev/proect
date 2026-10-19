"""Служебный бот логов: присылает разработчику ошибки и важные события ВСЕХ салонов.

Как устроено при нескольких салонах на одном сервере:
• менеджер (manager.py) — единственный, кто «слушает» бота логов: принимает /start, /status, /stop;
• каждый салон только ОТПРАВЛЯЕТ в него сообщения с пометкой [имя салона];
• чат, куда слать логи, хранится в shared/logbot.json — общий для всех.

Telegram не разрешает боту написать человеку первым — поэтому один раз нужно открыть
бота логов и нажать «Запустить». Подключиться может только LOG_OWNER из настроек.
"""
import asyncio
import html
import json
import logging
import time
import traceback
import urllib.request
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from . import config
from .config import LOG_OWNER, SHARED_DIR, TENANT, TENANT_DIR, TZ, runtime

events = logging.getLogger("lunka.events")  # важные события: events.info("📅 ...")
log = logging.getLogger("applog")

_queue: asyncio.Queue | None = None
_loop: asyncio.AbstractEventLoop | None = None
_started = time.time()
_stats = {"errors": 0, "warnings": 0, "last_error": ""}
router = Router()
STATUS_PROVIDER = None  # менеджер подставляет функцию, которая собирает статус всех салонов

NOISE = ("Failed to fetch updates", "Sleep for", "Connection established", "Unclosed client session")
CHAT_FILE = SHARED_DIR / "logbot.json"


def token() -> str:
    return config.LOG_BOT_TOKEN


# ---------- куда слать ----------
def chat_id() -> int | None:
    try:
        return int(json.loads(CHAT_FILE.read_text(encoding="utf-8")).get("chat_id") or 0) or None
    except Exception:
        return None


def set_chat_id(cid: int | None):
    SHARED_DIR.mkdir(exist_ok=True)
    CHAT_FILE.write_text(json.dumps({"chat_id": cid}), encoding="utf-8")


# ---------- перехват логов ----------
class TelegramHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            if any(n in msg for n in NOISE) or record.name.startswith("applog"):
                return
            if record.levelno >= logging.ERROR:
                _stats["errors"] += 1
                _stats["last_error"] = msg[:300]
                icon = "❌ "
            elif record.levelno >= logging.WARNING:
                _stats["warnings"] += 1
                icon = "⚠️ "
            else:
                icon = ""
            text = icon + msg
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
            stamp = datetime.fromtimestamp(record.created, TZ).strftime("%H:%M:%S")
            tag = f"<b>[{html.escape(TENANT)}]</b> " if TENANT else ""
            line = f"<code>{stamp}</code> {tag}{html.escape(text)}"
            if _loop and _queue is not None:
                _loop.call_soon_threadsafe(_queue.put_nowait, line)
        except Exception:
            pass


def install():
    if not token():
        return
    h = TelegramHandler()
    h.setLevel(logging.WARNING)
    logging.getLogger().addHandler(h)
    ev = TelegramHandler()
    ev.setLevel(logging.INFO)
    events.addHandler(ev)
    events.setLevel(logging.INFO)


async def _sender(bot: Bot):
    """Раз в 3 секунды отправляет накопившиеся строки одним сообщением, повторы склеивает."""
    while True:
        lines = [await _queue.get()]
        await asyncio.sleep(3)
        while not _queue.empty() and sum(len(x) for x in lines) < 3500:
            lines.append(_queue.get_nowait())
        cid = chat_id()
        if not cid:
            continue
        out, prev, rep = [], None, 0
        for ln in lines + [None]:
            body = ln.split("</code> ", 1)[-1] if ln else None
            if body is not None and body == prev:
                rep += 1
                continue
            if out and rep:
                out[-1] += f"  (×{rep + 1})"
            rep = 0
            if ln:
                out.append(ln)
            prev = body
        try:
            await bot.send_message(cid, "\n".join(out)[:4000], parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.debug("log send failed: %s", e)


# ---------- статус салона (файл status.json, его читает менеджер) ----------
def tenant_status() -> dict:
    from . import db
    today = db.now_local().date().isoformat()
    return {
        "salon": db.settings().get("salon_name", ""),
        "bot": runtime.get("bot_username") or "",
        "url": runtime.get("webapp_url") or "",
        "uptime_min": int((time.time() - _started) // 60),
        "masters": len(db.masters()),
        "clients": db.one("SELECT COUNT(*) AS n FROM clients")["n"],
        "bookings_today": db.one("SELECT COUNT(*) AS n FROM bookings WHERE status='active' AND start LIKE ?", (today + "%",))["n"],
        "created_today": db.one("SELECT COUNT(*) AS n FROM bookings WHERE created_at LIKE ?", (today + "%",))["n"],
        "errors": _stats["errors"], "warnings": _stats["warnings"], "last_error": _stats["last_error"],
        "updated": time.time(),
    }


async def _status_writer():
    while True:
        try:
            (TENANT_DIR / "status.json").write_text(json.dumps(tenant_status(), ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.debug("status write: %s", e)
        await asyncio.sleep(30)


def status_text() -> str:
    if STATUS_PROVIDER:
        return STATUS_PROVIDER()
    s = tenant_status()
    return (f"📊 <b>{html.escape(s['salon'])}</b>\nБот: @{s['bot'] or '—'}\nКабинет: {s['url'] or 'нет туннеля'}\n"
            f"Мастеров: {s['masters']} · клиентов: {s['clients']}\n"
            f"Записей сегодня: {s['bookings_today']} · создано сегодня: {s['created_today']}\n"
            f"Ошибок: {s['errors']} · предупреждений: {s['warnings']}")


# ---------- команды бота логов (только у менеджера или в одиночном режиме) ----------
def _allowed(message: Message) -> bool:
    return (message.from_user.username or "").lower() == LOG_OWNER


@router.message(CommandStart())
async def log_start(message: Message):
    if not _allowed(message):
        await message.answer("Это служебный бот проекта. Доступ закрыт.")
        return
    set_chat_id(message.chat.id)
    await message.answer("✅ <b>Логи подключены.</b>\n\nСюда приходят ошибки, предупреждения и события всех салонов "
                         "(запуск, падения и перезапуски, записи, отмены). Пометка в квадратных скобках — имя салона.\n\n"
                         "Команды:\n/status — состояние прямо сейчас\n/stop — отключить логи\n\n" + status_text())


@router.message(Command("status"))
async def log_status(message: Message):
    if _allowed(message):
        await message.answer(status_text())


@router.message(Command("stop"))
async def log_stop(message: Message):
    if _allowed(message):
        set_chat_id(None)
        await message.answer("Логи отключены. Чтобы включить снова — /start")


@router.message(F.text)
async def log_other(message: Message):
    if _allowed(message):
        await message.answer("Команды: /status — состояние, /stop — отключить логи.")


async def run(listen: bool):
    """listen=True — принимать команды (менеджер или одиночный режим); False — только отправлять (салон)."""
    global _queue, _loop
    tasks = [_status_writer()] if TENANT else []
    if token():
        _loop = asyncio.get_running_loop()
        _queue = asyncio.Queue()
        bot = Bot(token(), session=AiohttpSession(proxy=config.TG_PROXY),
                  default=DefaultBotProperties(parse_mode="HTML"))
        tasks.append(_sender(bot))
        if listen:
            try:
                me = await bot.get_me()
                dp = Dispatcher()
                dp.include_router(router)
                tasks.append(dp.start_polling(bot, handle_signals=False))
                logging.getLogger("lunka").info("Бот логов: https://t.me/%s%s", me.username,
                                                "" if chat_id() else f" — откройте его и нажмите «Запустить» (доступ: @{LOG_OWNER})")
            except Exception as e:
                logging.getLogger("lunka").warning("Бот логов не запустился (проверьте LOG_BOT_TOKEN): %s", e)
    if tasks:
        await asyncio.gather(*tasks)


def send_sync(text: str):
    """Отправить сообщение без asyncio — когда программа уже падает."""
    cid = chat_id()
    if not token() or not cid:
        return
    try:
        tag = f"<b>[{html.escape(TENANT)}]</b> " if TENANT else ""
        data = json.dumps({"chat_id": cid, "text": (tag + text)[:4000], "parse_mode": "HTML"}).encode()
        path = f"/bot{token()}/sendMessage"
        if config.TG_PROXY:  # через мост: свой сокет через SOCKS-прокси
            import http.client
            import ssl
            from python_socks.sync import Proxy
            host = "api.telegram.org"
            ctx = ssl.create_default_context()
            raw = Proxy.from_url(config.TG_PROXY).connect(dest_host=host, dest_port=443, timeout=10)
            conn = http.client.HTTPSConnection(host, timeout=10, context=ctx)
            conn.sock = ctx.wrap_socket(raw, server_hostname=host)
            conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
            conn.getresponse().read()
            conn.close()
            return
        req = urllib.request.Request("https://api.telegram.org" + path, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
