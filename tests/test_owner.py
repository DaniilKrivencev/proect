"""Владелец, назначенный из бота техподдержки (@username или ссылка): python -m tests.test_owner"""
import asyncio
import os
import tempfile
from datetime import datetime

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="0", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from fastapi.testclient import TestClient

from pathlib import Path
from app import adminbot, bot as bot_module, db, notify, reviews

OWNER, DEV, CLIENT = 111, 999, 222


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if text:
            self.sent.append((getattr(method, "chat_id", None), name, text, getattr(method, "reply_markup", None)))
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Лунка", username="lunka_test_bot")
        if name in ("SendMessage", "EditMessageText"):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"), text=text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""


def user(uid, name="Анна"):
    return User(id=uid, is_bot=False, first_name=name, username=f"user{uid}")


def msg(uid, text=None, contact=None, photo=None):
    return Update(update_id=1, message=Message(message_id=5, date=datetime.now(), chat=Chat(id=uid, type="private"),
                                               from_user=user(uid), text=text, contact=contact, photo=photo))


def cb(uid, data):
    return Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=user(uid), chat_instance="x", data=data,
        message=Message(message_id=7, date=datetime.now(), chat=Chat(id=uid, type="private"), text="old")))
JPEG = b"\xff\xd8\xff\xe0fakejpeg"




DBP = Path(os.environ["DB_FILE"])
TEN = Path(tmp) / "tenants"
(TEN / "maria").mkdir(parents=True)
(TEN / "maria" / "tenant.env").write_text(f"NAME=Стрижка у Марии\nNICHE=hair\nDB_FILE={DBP}\n", encoding="utf-8")
adminbot.TENANTS = TEN
adminbot._allowed = lambda e: True
adminbot._BOTS["maria"] = "maria_test_bot"


async def main():
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    adm = Dispatcher(storage=MemoryStorage())
    adm.include_router(adminbot.router)

    # 0. База салона ещё не создана — назначение всё равно сохраняется
    await adm.feed_update(bot, cb(DEV, "dv:ow:maria:solo"))
    await adm.feed_update(bot, msg(DEV, "@User333"))
    out = session.sent[-1][2]
    assert "@user333" in out and "start=owner_" in out, out

    db.init()
    notify.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(reviews.router)
    dp.include_router(bot_module.router)
    st = db.settings()
    assert st["owner_username"] == "user333" and st["owner_mode"] == "solo" and st["owner_code"]

    # 1. Посторонний клиент не видит «подключить салон» и не становится владельцем
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, "/start"))
    assert not any("reg:" in str(s[3]) for s in session.sent), "чужому предложили подключить салон"
    await dp.feed_update(bot, cb(CLIENT, "reg:salon"))
    assert not db.admins()

    # 2. Заказчик с этим @username нажал «Запустить» — сразу владелец, частный мастер
    await dp.feed_update(bot, msg(333, "/start"))
    assert db.is_admin(333) and db.is_solo() and db.own_master(333)
    st = db.settings()
    assert not st["owner_username"] and not st["owner_code"], "ожидание не сброшено"

    # 3. Новое назначение по ссылке, формат «салон»: сработает только верный код и один раз
    session.sent.clear()
    await adm.feed_update(bot, cb(DEV, "dv:own:maria"))
    assert "Анна" in session.sent[-1][2], "в меню видно текущего владельца"
    await adm.feed_update(bot, cb(DEV, "dv:ow:maria:salon"))
    await adm.feed_update(bot, cb(DEV, "dv:ow_link"))
    code = db.settings()["owner_code"]
    assert code and not db.settings()["owner_username"]
    await dp.feed_update(bot, msg(444, "/start owner_0000"))
    assert not db.is_admin(444)
    await dp.feed_update(bot, msg(444, f"/start owner_{code}"))
    assert db.is_admin(444) and not db.is_solo()
    await dp.feed_update(bot, msg(CLIENT, f"/start owner_{code}"))
    assert not db.is_admin(CLIENT), "ссылка сработала второй раз"

    # 4. Неверный username и отмена ожидания
    await adm.feed_update(bot, cb(DEV, "dv:ow:maria:salon"))
    await adm.feed_update(bot, msg(DEV, "привет"))
    assert "не похоже" in session.sent[-1][2]
    await adm.feed_update(bot, msg(DEV, "https://t.me/new_owner"))
    assert db.settings()["owner_username"] == "new_owner"
    await adm.feed_update(bot, cb(DEV, "dv:ow_clear:maria"))
    assert not db.settings()["owner_code"]
    print("✅ Владелец из бота техподдержки: по @username и по ссылке, формат, одноразовость, защита от чужих")


asyncio.run(main())
