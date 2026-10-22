"""Подготовка салона к выдаче заказчику из бота техподдержки: python -m tests.test_clean"""
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



adminbot.SHARED_DIR = Path(tempfile.mkdtemp())
adminbot.BACKUP = lambda: Path(tmp) / "backup_test.zip"


async def main():
    db.init()
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    notify.bot = bot
    adm = Dispatcher(storage=MemoryStorage())
    adm.include_router(adminbot.router)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(reviews.router)
    dp.include_router(bot_module.router)

    # Тестовые данные: я — владелец и мастер, клиент, запись, чат, отзыв с фото
    db.add_admin(DEV, "Я")
    db.ex("DELETE FROM masters")
    mid = db.create_master("Зарина", "топ", DEV)["id"]
    sid = db.services()[0]["id"]
    db.ex("INSERT OR IGNORE INTO master_services VALUES (?,?)", (mid, sid))
    cid = db.ex("INSERT INTO clients (tg_id, name, phone, created_at) VALUES (?,?,?,?)", (CLIENT, "Тест", "+7900", db.now_str()))
    bid = db.ex("INSERT INTO bookings (master_id, client_id, service_id, service_name, start, duration, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (mid, cid, sid, "x", "2026-10-01 10:00", 60, db.now_str()))
    db.ex("INSERT INTO messages (client_id, direction, text, created_at) VALUES (?,?,?,?)", (cid, "in", "привет", db.now_str()))
    photo = DBP.parent / "reviews" / ("a" * 24 + ".jpg")
    photo.parent.mkdir(exist_ok=True)
    photo.write_bytes(JPEG)
    db.ex("INSERT INTO reviews (booking_id, master_id, client_id, rating, photo, created_at) VALUES (?,?,?,?,?,?)",
          (bid, mid, cid, 5, photo.name, db.now_str()))
    db.set_setting("dev_mode", "client")
    db.set_setting("mode", "solo")
    db.set_setting("address", "ул. Мира, 1")
    old_invite = db.get_master(mid)["invite"]

    # Через бот техподдержки: кнопка → PIN → выбор → очистка
    await adm.feed_update(bot, cb(DEV, "dv:clean:maria"))
    await adm.feed_update(bot, msg(DEV, "1234"))   # придумал PIN
    await adm.feed_update(bot, msg(DEV, "1234"))   # подтвердил
    preview = session.sent[-1][2]
    assert "записи — 1" in preview and "отзывы (и их фото) — 1" in preview and "мастеров (1)" in preview, preview
    await adm.feed_update(bot, cb(DEV, "dv:cl:unlink"))
    done = session.sent[-1][2]
    assert "чистый" in done and "backup_test.zip" in done, done
    assert "dv:ow:maria:solo" in str(session.sent[-1][3]), "после очистки сразу предлагает назначить владельца"

    for t in ("bookings", "clients", "messages", "reviews", "admins"):
        assert not db.q(f"SELECT 1 FROM {t}"), t
    assert not photo.exists(), "фото отзывов не удалены"
    m = db.get_master(mid)
    assert m and m["tg_id"] is None and m["invite"] != old_invite, "мастер не отвязан"
    assert db.q("SELECT 1 FROM master_services WHERE master_id=?", (mid,)), "услуги мастера пропали"
    st = db.settings()
    assert st["address"] == "ул. Мира, 1" and st["dev_mode"] == "" and st["mode"] == "" and st["salon_name"]

    # Назначаю владельца «частный мастер» — он становится ЭТИМ мастером, а не вторым
    await adm.feed_update(bot, cb(DEV, "dv:ow:maria:solo"))
    await adm.feed_update(bot, msg(DEV, "@user333"))
    await dp.feed_update(bot, msg(333, "/start"))
    assert db.is_admin(333) and db.is_solo()
    assert [x["id"] for x in db.masters()] == [mid] and db.own_master(333)["id"] == mid
    print("✅ Подготовка к выдаче: тестовые данные стёрты, мастера и услуги остались, владелец назначен")


asyncio.run(main())
