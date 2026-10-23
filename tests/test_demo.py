"""Демо-салон: роли, сообщения «другим» уходят самому посетителю, ограничения, ночной сброс. python -m tests.test_demo"""
import asyncio
import os
import tempfile
from datetime import datetime

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="0", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off", DEMO="1", LUNKA_TENANT_DIR=tmp)

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from fastapi.testclient import TestClient

from app import api, bot as bot_module, db, demo, notify, reviews

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
    demo.ensure_ready()
    assert len(db.masters()) == 3 and db.q("SELECT 1 FROM reviews"), "демо не заполнился"
    assert db.one("SELECT 1 FROM bookings WHERE start>=?", (db.now_str(),)), "нет записей на неделю"
    assert demo.SNAPSHOT.exists()
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    notify.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(demo.actor_mw)
    dp.include_router(demo.router)
    dp.include_router(reviews.router)
    dp.include_router(bot_module.router)
    A, B = 301, 302

    # 1. Новый посетитель видит выбор роли, а не «подключить салон»
    await dp.feed_update(bot, msg(A, "/start"))
    kbs = str(session.sent[-1][3])
    assert "demo:client" in kbs and "demo:owner" in kbs and "reg:" not in kbs
    await dp.feed_update(bot, cb(A, "reg:salon"))
    assert not db.admins()
    # приглашения в демо не действуют
    inv = db.masters()[0]["invite"]
    await dp.feed_update(bot, msg(A, f"/start staff_{inv}"))
    assert db.masters()[0]["tg_id"] is None

    # 2. Клиент: B пишет салону — получает пояснение, никому не уходит
    await dp.feed_update(bot, cb(B, "demo:client"))
    assert not db.is_admin(B)
    session.sent.clear()
    await dp.feed_update(bot, msg(B, "Здравствуйте, есть окошко?"))
    assert all(s[0] == B for s in session.sent) and "Демо" in session.sent[-1][2]
    db.ex("UPDATE clients SET phone='+7 999 123-45-67' WHERE tg_id=?", (B,))

    # 3. Владелец: кабинет, телефоны скрыты, опасное запрещено, ответ «другому» приходит самому
    await dp.feed_update(bot, cb(A, "demo:owner"))
    assert db.is_admin(A)
    from types import SimpleNamespace
    tc = TestClient(api.app)
    api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=A, first_name="А", last_name=None))
    H = {"X-Init-Data": "x"}
    me = tc.get("/api/me", headers=H).json()
    assert me["role"] == "admin" and me["demo"] is True
    clients = tc.get("/api/clients", headers=H).json()
    flat = str(clients)
    assert "123-45-67" not in flat and "999" not in flat.replace("+7 900", ""), "телефон посетителя виден в демо"
    assert tc.get("/api/admin_invite", headers=H).status_code == 403
    assert tc.delete(f"/api/masters/{db.masters()[0]['id']}", headers=H).status_code == 403
    assert tc.post("/api/mode", headers=H, json={"mode": "solo"}).status_code == 403
    cid = db.one("SELECT id FROM clients WHERE tg_id=?", (B,))["id"]
    session.sent.clear()
    r = tc.post(f"/api/chats/{cid}", headers=H, json={"text": "Привет от салона"})
    assert r.status_code == 200, r.text
    assert session.sent and all(s[0] == A for s in session.sent), session.sent
    assert "получил бы клиент" in session.sent[-1][2]

    # 4. Мастер смотрит глазами первого мастера
    await dp.feed_update(bot, cb(A, "demo:master"))
    assert db.master_by_tg(A)["id"] == db.masters()[0]["id"] and not db.is_admin(A)

    # 5. Запись клиента: «уведомление мастеру» показывается самому клиенту
    b = db.one(db.BOOKING_SQL + " WHERE b.start>=? LIMIT 1", (db.now_str(),))
    session.sent.clear()
    tok = notify.ACTOR.set(B)
    await notify.staff_new_booking(b)
    notify.ACTOR.reset(tok)
    assert session.sent and session.sent[-1][0] == B and "мастер и владелец" in session.sent[-1][2]

    # 6. Ночной сброс: следы посетителей стёрты, демо-данные и роли на месте
    db.ex("UPDATE masters SET name='Испорчено' WHERE id=?", (db.masters()[0]["id"],))
    demo.reset()
    assert db.masters()[0]["name"] == "Анна"
    assert not db.q("SELECT 1 FROM clients WHERE tg_id IS NOT NULL") and not db.q("SELECT 1 FROM messages")
    assert db.demo_role(A) == "master" and db.demo_role(B) == "client"
    assert db.one("SELECT 1 FROM bookings WHERE start>=?", (db.now_str(),))
    print("✅ Демо-салон: выбор роли, сообщения не уходят другим, телефоны скрыты, опасное закрыто, ночной сброс")


asyncio.run(main())
