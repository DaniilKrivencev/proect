"""Уведомления: мастеру, владельцу салона и клиентам."""
import html
import logging
from contextvars import ContextVar

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config, db
from .niche import N
from .texts import start_human

log = logging.getLogger("notify")
bot: Bot | None = None  # задаётся при запуске
# Демо-салон: кто сейчас нажимает кнопки. Сообщения «другим людям» в демо уходят ему самому —
# посетители демо не могут писать друг другу от имени салона.
ACTOR: ContextVar[int | None] = ContextVar("actor", default=None)
DEMO_CLIENT = "👀 <i>Демо: это сообщение получил бы клиент</i>\n\n"
DEMO_STAFF = "👀 <i>Демо: так это сообщение увидят мастер и владелец</i>\n\n"


class MCancel(CallbackData, prefix="mc"):
    b: int
    sure: int = 0


class CCancel(CallbackData, prefix="cc"):
    b: int
    sure: int = 0


async def send(chat_id: int | None, text: str, kb=None) -> bool:
    if config.DEMO:
        actor = ACTOR.get()
        if actor and chat_id != actor:
            chat_id, text, kb = actor, DEMO_CLIENT + text, None
    if not chat_id or bot is None:
        return False
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        return True
    except Exception as e:  # заблокировал бота и т.п.
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


def staff_chats(b: dict) -> list[int]:
    """Кому из салона сообщать о записи: мастеру и владельцам (без повторов)."""
    chats = [b.get("master_tg")] + [a["tg_id"] for a in db.admins()]
    seen, out = set(), []
    for c in chats:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def booking_line(b: dict) -> str:
    return f"<b>{start_human(b['start'])}</b>\n{b['service_name']}"


async def staff_new_booking(b: dict):
    contact = f"\n📱 {b['client_phone']}" if b.get("client_phone") else ""
    note = f"\n📝 {b['client_note']}" if b.get("client_note") else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отменить запись", callback_data=MCancel(b=b["id"]).pack())]])
    from .applog import events
    events.info("📅 Новая запись: %s, %s, %s", b["master_name"], b["service_name"], start_human(b["start"]))
    text = (f"🌸 Новая запись к мастеру {b['master_name']}\n\n{booking_line(b)}\n"
            f"👤 {b['client_name']}{contact}{note}")
    for chat in staff_chats(b):
        await send(chat, text, kb)
    await demo_staff_echo(text)


async def staff_cancelled_by_client(b: dict):
    from .applog import events
    events.info("❌ Клиентка отменила запись: %s, %s", b["master_name"], start_human(b["start"]))
    text = (f"❌ Клиент отменил запись к мастеру {b['master_name']}\n\n{booking_line(b)}\n👤 {b['client_name']}\n\n"
            f"Время снова свободно для записи.")
    for chat in staff_chats(b):
        await send(chat, text)
    await demo_staff_echo(text)


async def demo_staff_echo(text: str):
    """Демо: показать посетителю-клиенту, что получил бы мастер (настоящих сотрудников в демо нет)."""
    actor = ACTOR.get()
    if not (config.DEMO and actor and bot):
        return
    try:
        await bot.send_message(actor, DEMO_STAFF + text +
                               f"\n\n💡 Хотите так же у себя? Пишите → @{config.LOG_OWNER}")
    except Exception as e:
        log.warning("demo echo: %s", e)


async def client_cancelled_by_salon(b: dict):
    from .applog import events
    events.info("❌ Салон отменил запись: %s, %s", b["master_name"], start_human(b["start"]))
    salon = db.settings()["salon_name"]
    await send(b.get("client_tg"), f"😔 К сожалению, {salon} отменил вашу запись:\n\n{booking_line(b)}\n"
                                   f"Мастер: {b['master_name']}\n\nЧтобы выбрать другое время, нажмите /start")


async def invite_back(client: dict) -> bool:
    from .config import runtime
    salon = db.settings()["salon_name"]
    return await send(client["tg_id"],
                      f"Здравствуйте, {client['name']}! 🌸\n\nДавно вас не видели в «{salon}». "
                      f"{N['come_back']} — свободные окошки здесь:\n"
                      f"https://t.me/{runtime['bot_username']}")


class Reply(CallbackData, prefix="rp"):  # «Ответить клиенту»
    c: int


def reply_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Ответить", callback_data=Reply(c=client_id).pack())]])


async def salon_reply(client: dict, text: str, author: str) -> bool:
    """Отправить клиенту ответ салона и сохранить его в переписке."""
    salon = db.settings()["salon_name"]
    ok = await send(client["tg_id"], f"💬 <b>Сообщение от салона «{html.escape(salon)}»</b>\n\n{html.escape(text)}")
    if ok:
        db.log_message(client["id"], "out", text, author=author)
    return ok
