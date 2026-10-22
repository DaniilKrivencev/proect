"""Формат работы: частный мастер или салон; техподдержка не может подключить салон. python -m tests.test_solo"""
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

from app import api, bot as bot_module, db, notify, reviews

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



async def main():
    db.init()
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    notify.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(reviews.router)
    dp.include_router(bot_module.router)

    # 1. Техподдержка в роли клиента не видит «подключить» и не может стать владельцем
    db.dev_id = lambda: DEV
    db.set_setting("dev_mode", "client")
    await dp.feed_update(bot, msg(DEV, "/start"))
    assert not any("reg:" in str(s[3]) for s in session.sent), "техподдержке предложили подключить салон"
    await dp.feed_update(bot, cb(DEV, "reg:salon"))
    assert not db.admins(), "техподдержка стала владельцем"
    db.ex("DELETE FROM settings WHERE key='dev_mode'")

    # 2. Владелец видит выбор из двух и выбирает «сам на себя»
    session.sent.clear()
    await dp.feed_update(bot, msg(OWNER, "/start"))
    kb_text = str(session.sent[-1][3])
    assert "reg:solo" in kb_text and "reg:salon" in kb_text, kb_text
    await dp.feed_update(bot, cb(OWNER, "reg:solo"))
    assert db.is_admin(OWNER) and db.is_solo()
    me_master = db.own_master(OWNER)
    assert me_master and [m["id"] for m in db.masters()] == [me_master["id"]], "частный мастер = единственный мастер"
    assert len(db.q("SELECT 1 FROM master_services WHERE master_id=?", (me_master["id"],))) == len(db.services()), "все услуги отмечены"
    assert any("Ваш кабинет" in s[2] for s in session.sent), "домашний экран частного мастера"

    # 3. Клиент записывается без шага «выбор мастера»
    assert not bot_module.has_master_step(db.services()[0]["id"])

    # 4. Кабинет: формат и свой мастер; смена формата
    tc = TestClient(api.app)
    api_parse = api.safe_parse_webapp_init_data
    from types import SimpleNamespace
    api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=OWNER, first_name="Анна", last_name=None))
    H = {"X-Init-Data": "x"}
    me = tc.get("/api/me", headers=H).json()
    assert me["mode"] == "solo" and me["own_master_id"] == me_master["id"] and me["role"] == "admin"
    assert tc.post("/api/mode", headers=H, json={"mode": "salon"}).json()["mode"] == "salon"
    db.create_master("Оля")
    r = tc.post("/api/mode", headers=H, json={"mode": "solo"})
    assert r.status_code == 409 and "Оля" in r.json()["detail"], "нельзя стать частным мастером, пока есть другие мастера"
    assert tc.post("/api/mode", headers=H, json={"mode": "bad"}).status_code == 422
    api.safe_parse_webapp_init_data = api_parse

    # 5. Второй человек уже не может подключить
    await dp.feed_update(bot, cb(CLIENT, "reg:solo"))
    assert not db.is_admin(CLIENT)
    print("✅ Формат работы: частный мастер и салон, смена формата, защита от техподдержки")


asyncio.run(main())
