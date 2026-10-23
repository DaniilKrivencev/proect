"""Демо-салон: у каждого посетителя своя песочница, друг другу не мешают. python -m tests.test_demo"""
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
    from types import SimpleNamespace
    tc = TestClient(api.app)
    who = {"id": A}
    api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=who["id"], first_name="X", last_name=None))
    H = {"X-Init-Data": "x"}

    def in_box(uid, fn):
        tok = db.use_conn(demo.sandbox_for(uid))
        try:
            return fn()
        finally:
            db.reset_conn(tok)

    # 1. Выбор роли, «подключить салон» не предлагается
    await dp.feed_update(bot, msg(A, "/start"))
    kbs = str(session.sent[-1][3])
    assert "demo:client" in kbs and "demo:owner" in kbs and "reg:" not in kbs
    # 2. У каждого — своя копия с мастерами и записями на неделю
    assert in_box(A, lambda: len(db.masters())) == 3 and in_box(B, lambda: len(db.masters())) == 3
    assert in_box(B, lambda: db.one("SELECT 1 FROM bookings WHERE start>=?", (db.now_str(),)))

    # 3. A — владелец: меняет всё у себя, B и общий салон не затронуты
    await dp.feed_update(bot, cb(A, "demo:owner"))
    await dp.feed_update(bot, cb(B, "demo:client"))
    assert in_box(A, lambda: db.is_admin(A)) and not in_box(B, lambda: db.is_admin(B))
    me = tc.get("/api/me", headers=H).json()
    assert me["role"] == "admin" and me["demo"] is True, me
    mid = in_box(A, lambda: db.masters()[0]["id"])
    assert tc.delete(f"/api/masters/{mid}", headers=H).status_code in (200, 409), "удаление мастера в своей песочнице"
    sid = in_box(A, lambda: db.services()[0]["id"])
    tc.delete(f"/api/services/{sid}", headers=H)
    assert in_box(B, lambda: len(db.services())) == len(db.services()), "услуги B пострадали"
    assert len(db.masters()) == 3, "общий салон изменился"
    assert tc.get("/api/admin_invite", headers=H).status_code == 403

    # 4. B пишет «салону» — сообщение в ЕГО песочнице, A его не видит
    await dp.feed_update(bot, msg(B, "Здравствуйте, есть окошко?"))
    assert in_box(B, lambda: db.q("SELECT 1 FROM messages")) and not in_box(A, lambda: db.q("SELECT 1 FROM messages"))
    assert tc.get("/api/chats", headers=H).json() in ([], {"chats": []}) or "окошко" not in str(tc.get("/api/chats", headers=H).json())
    # B как владелец видит свою переписку и отвечает сам себе
    await dp.feed_update(bot, cb(B, "demo:owner"))
    who["id"] = B
    cid = in_box(B, lambda: db.one("SELECT id FROM clients WHERE tg_id=?", (B,))["id"])
    session.sent.clear()
    assert tc.post(f"/api/chats/{cid}", headers=H, json={"text": "Есть, в 15:00"}).status_code == 200
    assert session.sent and session.sent[-1][0] == B and "Есть, в 15:00" in session.sent[-1][2]

    # 4б. «Попросить отзыв» в песочнице: публичная страница находит запись посетителя, фото открывается
    from app.config import runtime
    runtime["webapp_url"] = "https://demo.example"
    past = in_box(B, lambda: db.one("SELECT id FROM bookings WHERE start<? AND status='active' "
                                    "AND id NOT IN (SELECT booking_id FROM reviews) LIMIT 1", (db.now_str(),)))
    if not past:
        mid_b = in_box(B, lambda: db.masters()[0]["id"])
        past = {"id": in_box(B, lambda: db.ex("INSERT INTO bookings (master_id, client_id, service_name, start, duration, created_at) "
                                               "VALUES (?,?,?,?,?,?)", (mid_b, cid, "x", "2026-01-01 10:00", 60, db.now_str())))}
    link = tc.get(f"/api/bookings/{past['id']}/review_link", headers=H).json()["link"]
    t = link.split("t=")[1]
    assert tc.get(f"/api/public/review/{t}").status_code == 200
    import base64
    ph = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0demo").decode()
    assert tc.post(f"/api/public/review/{t}", json={"rating": 5, "text": "супер", "photo": ph}).status_code == 200
    name = in_box(B, lambda: db.one("SELECT photo FROM reviews WHERE booking_id=?", (past["id"],))["photo"])
    assert name and tc.get(f"/r/{name}").status_code == 200
    assert not db.q("SELECT 1 FROM reviews WHERE text='супер'"), "отзыв попал в общий салон"

    # 5. Уведомление «мастеру» о записи вымышленной клиентки приходит самому посетителю
    b = in_box(B, lambda: db.one(db.BOOKING_SQL + " WHERE b.start>=? LIMIT 1", (db.now_str(),)))
    session.sent.clear()
    tok = notify.ACTOR.set(B)
    await notify.staff_new_booking(b)
    notify.ACTOR.reset(tok)
    assert session.sent and session.sent[-1][0] == B and "мастер и владелец" in session.sent[-1][2]

    # 6. «Начать заново» — копия как новая; мастер — глазами первого мастера
    await dp.feed_update(bot, cb(A, "demo:restart"))
    assert in_box(A, lambda: len(db.masters())) == 3 and not in_box(A, lambda: db.demo_role(A))
    await dp.feed_update(bot, cb(A, "demo:master"))
    assert in_box(A, lambda: db.master_by_tg(A))["name"] == "Анна"

    # 7. 20 человек одновременно: у каждого своя роль и своя копия
    users = list(range(1000, 1020))
    await asyncio.gather(*(dp.feed_update(bot, cb(u, "demo:owner" if u % 2 else "demo:client")) for u in users))
    for u in users:
        assert in_box(u, lambda: db.demo_role(u)) == ("owner" if u % 2 else "client")
        assert in_box(u, lambda: [r["tg_id"] for r in db.q("SELECT tg_id FROM demo_roles")]) == [u]
    assert len(list(demo.SANDBOXES.glob("*.db"))) >= 22

    # 8. Сброс из бота техподдержки — все песочницы заново; давние — удаляются
    demo.FLAG_RESET.write_text("1")
    demo.tick()
    assert not list(demo.SANDBOXES.glob("*.db"))
    print("✅ Демо: у каждого своя песочница — меняют что угодно, друг друга не видят и не мешают")


asyncio.run(main())
