"""Полный сценарий записи через MAX (сервер MAX имитируется): python -m tests.test_max"""
import asyncio
import json
import os
import tempfile
from datetime import datetime

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="111", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off")

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from fastapi.testclient import TestClient

from app import api, bot as bot_module, config, db, maxbot, notify, reminders
from datetime import timedelta

OWNER = 111            # владелец — в Telegram
MAX_CLIENT = 5005      # клиентка — в MAX (в базе будет -5005)
MAX_MASTER = 6006      # мастер — в MAX


# ---------- имитация сервера MAX ----------
class FakeMax:
    def __init__(self):
        self.sent, self.n = [], 0

    def handler(self, req: httpx.Request):
        path, q = req.url.path, dict(req.url.params)
        body = json.loads(req.content) if req.content else None
        if path == "/me":
            return httpx.Response(200, json={"user_id": 900, "first_name": "Салон", "username": "salon_max_bot", "is_bot": True})
        if path == "/messages" and req.method == "POST":
            self.n += 1
            self.sent.append(("send", int(q["user_id"]), body))
            return httpx.Response(200, json={"message": {"body": {"mid": f"mid{self.n}", "seq": self.n}, "recipient": {"chat_type": "dialog"}, "timestamp": 1}})
        if path == "/messages" and req.method == "PUT":
            self.sent.append(("edit", q["message_id"], body))
            return httpx.Response(200, json={"success": True})
        if path == "/answers":
            self.sent.append(("answer", q["callback_id"], body))
            return httpx.Response(200, json={"success": True})
        if path == "/me/commands":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "nope"})

    def last_to(self, uid):
        for kind, who, body in reversed(self.sent):
            if kind == "send" and who == uid or kind == "edit":
                return body
        raise AssertionError("нет сообщений")

    def texts_to(self, uid):
        return [b.get("text") or "" for k, w, b in self.sent if k == "send" and w == uid]

    def buttons(self, body):
        for a in body.get("attachments") or []:
            if a["type"] == "inline_keyboard":
                return [b for row in a["payload"]["buttons"] for b in row]
        return []


class FakeTG(maxbot.RoutingSession):
    """Telegram-сторона: вместо интернета — запись в список."""
    def __init__(self):
        super().__init__()
        self.sent = []

    async def _tg(self, bot, method, timeout=None):
        name = type(method).__name__
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        self.sent.append((getattr(method, "chat_id", None), name, text or "", getattr(method, "reply_markup", None)))
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="TG", username="salon_tg_bot")
        if name in ("SendMessage", "SendPhoto", "EditMessageText"):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=text)
        return True


def max_update(kind, **kw):
    return kw | {"update_type": kind, "timestamp": 1}


def user(uid, name="Анна"):
    return {"user_id": uid, "first_name": name, "is_bot": False, "username": None}


async def main():
    db.init()
    fake = FakeMax()
    msession = maxbot.MaxSession("MAXTOKEN", http=httpx.AsyncClient(base_url=maxbot.API, transport=httpx.MockTransport(fake.handler)))
    tg_session = FakeTG()
    tg = Bot(config.BOT_TOKEN, session=tg_session, default=DefaultBotProperties(parse_mode="HTML"))
    msession.tg_bot = tg
    max_bot = Bot("1:max", session=msession)
    tg_session.max_session, tg_session.max_bot = msession, max_bot
    notify.bot = tg
    config.runtime.update(bot_username="salon_tg_bot", webapp_url="https://cab.example", max_username="salon_max_bot")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(bot_module.router)

    async def feed(u):
        upd = maxbot.convert(u, msession)
        assert upd is not None, u
        await dp.feed_update(max_bot, upd)

    async def press(uid, payload, mid="mid1"):
        await feed(max_update("message_callback", callback={"callback_id": f"cb{len(fake.sent)}", "payload": payload,
                                                           "user": user(uid), "timestamp": 1},
                              message={"body": {"mid": mid, "seq": 1, "text": "x"}, "recipient": {"chat_type": "dialog"}, "timestamp": 1}))

    def find(body, text):
        for b in fake.buttons(body):
            if text in b["text"]:
                return b
        raise AssertionError(f"нет кнопки «{text}»: {[b['text'] for b in fake.buttons(body)]}")

    # 1. Владелец подключает салон в Telegram, добавляет двух мастеров
    await dp.feed_update(tg, Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=User(id=OWNER, is_bot=False, first_name="Зарина"), chat_instance="x", data="reg",
        message=Message(message_id=1, date=datetime.now(), chat=Chat(id=OWNER, type="private"), text="x"))))
    assert db.is_admin(OWNER)
    c = TestClient(api.app)
    H = {"X-Init-Data": "dev"}
    for name in ("Аня", "Катя"):
        m = c.post("/api/masters", headers=H, json={"name": name}).json()
        c.put(f"/api/masters/{m['id']}", headers=H, json={"schedule": [{"weekday": i, "works": True, "start": "10:00", "end": "20:00"} for i in range(7)]})
    assert "max.ru/salon_max_bot?start=staff_" in m["invite_link_max"]

    # 2. Клиентка в MAX нажимает «Начать»
    await feed(max_update("bot_started", chat_id=1, user=user(MAX_CLIENT), payload=None))
    welcome = [b for k, w, b in fake.sent if k == "send" and w == MAX_CLIENT]
    menu = fake.buttons(welcome[0])
    assert {b["type"] for b in menu} == {"message"}, "меню должно быть кнопками-сообщениями"
    assert any("Записаться" in b["text"] for b in menu)
    book = find(welcome[-1], "Записаться")
    assert book["type"] == "callback"

    # 3. Запись: услуга → любой мастер → день → время
    await press(MAX_CLIENT, book["payload"])
    services = fake.last_to(MAX_CLIENT)
    assert "Шаг 1" in services["text"] and services["format"] == "html"
    await press(MAX_CLIENT, find(services, "гель-лаком")["payload"])
    await press(MAX_CLIENT, find(fake.last_to(MAX_CLIENT), "Любой")["payload"])
    days = fake.last_to(MAX_CLIENT)
    assert "Шаг 3 из 4" in days["text"]
    await press(MAX_CLIENT, fake.buttons(days)[1]["payload"])
    times = fake.last_to(MAX_CLIENT)
    slot = next(b for b in fake.buttons(times) if b["payload"].startswith("t:"))
    await press(MAX_CLIENT, slot["payload"])
    phone_req = fake.last_to(MAX_CLIENT)
    assert "номер телефона" in phone_req["text"]
    assert any(b["type"] == "request_contact" for b in fake.buttons(phone_req)), "нет кнопки отправки контакта"

    # 4. Клиентка делится контактом — запись создана, владелец в Telegram получил уведомление
    await feed(max_update("message_created", message={
        "sender": user(MAX_CLIENT), "recipient": {"chat_type": "dialog", "user_id": 900}, "timestamp": 1,
        "body": {"mid": "in1", "seq": 5, "text": None, "attachments": [{"type": "contact", "payload": {
            "vcf_info": "BEGIN:VCARD\nVERSION:3.0\nFN:Анна\nTEL;TYPE=cell:79001234567\nEND:VCARD", "max_info": user(MAX_CLIENT)}}]}}))
    assert any("Вы записаны" in t for t in fake.texts_to(MAX_CLIENT)), fake.texts_to(MAX_CLIENT)[-2:]
    b = db.one("SELECT * FROM bookings")
    client = db.one("SELECT * FROM clients WHERE id=?", (b["client_id"],))
    assert client["tg_id"] == -MAX_CLIENT and client["phone"] == "+79001234567"
    assert any(chat == OWNER and "Новая запись" in t for chat, _, t, _ in tg_session.sent), "владелец в Telegram не узнал о записи"
    print("✅ Запись через MAX:", b["start"], b["service_name"])

    # 5. Кнопка меню «Мои записи» (в MAX это кнопка, которая отправляет текст)
    await feed(max_update("message_created", message={"sender": user(MAX_CLIENT), "recipient": {"chat_type": "dialog"}, "timestamp": 1,
                                                       "body": {"mid": "in2", "seq": 6, "text": bot_module.B_MY}}))
    my = fake.last_to(MAX_CLIENT)
    assert find(my, "Перенести") and find(my, "Отменить")

    # 6. Вопрос клиентки → владельцу в Telegram; владелец отвечает → клиентке в MAX
    await feed(max_update("message_created", message={"sender": user(MAX_CLIENT), "recipient": {"chat_type": "dialog"}, "timestamp": 1,
                                                       "body": {"mid": "in3", "seq": 7, "text": "Есть парковка?"}}))
    assert any(chat == OWNER and "Есть парковка?" in t for chat, _, t, _ in tg_session.sent)
    reply_btn = next(m.inline_keyboard[0][0] for chat, _, t, m in tg_session.sent if chat == OWNER and "парковка" in t)
    await dp.feed_update(tg, Update(update_id=2, callback_query=CallbackQuery(
        id="2", from_user=User(id=OWNER, is_bot=False, first_name="Зарина"), chat_instance="x", data=reply_btn.callback_data,
        message=Message(message_id=1, date=datetime.now(), chat=Chat(id=OWNER, type="private"), text="x"))))
    await dp.feed_update(tg, Update(update_id=3, message=Message(message_id=50, date=datetime.now(), chat=Chat(id=OWNER, type="private"),
                                                                 from_user=User(id=OWNER, is_bot=False, first_name="Зарина"), text="Да, во дворе 🚗")))
    assert any("Да, во дворе" in t for t in fake.texts_to(MAX_CLIENT)), "ответ владельца не дошёл в MAX"

    # 7. Фото из MAX доходит владельцу в Telegram
    await dp.feed_update(tg, Update(update_id=4, message=Message(message_id=51, date=datetime.now(), chat=Chat(id=OWNER, type="private"),
                                                                 from_user=User(id=OWNER, is_bot=False, first_name="Зарина"), text="✖️ Закончить ответ")))
    await feed(max_update("message_created", message={"sender": user(MAX_CLIENT), "recipient": {"chat_type": "dialog"}, "timestamp": 1,
                                                       "body": {"mid": "in4", "seq": 8, "text": "вот такой дизайн",
                                                                "attachments": [{"type": "image", "payload": {"token": "T0K", "url": "https://i.max/x.jpg", "photo_id": 1}}]}}))
    assert any(name == "SendPhoto" and chat == OWNER for chat, name, _, _ in tg_session.sent), "фото не дошло в Telegram"

    # 8. Напоминание за сутки уходит клиентке в MAX
    start = db.now_local() + timedelta(hours=23)
    db.ex("UPDATE bookings SET start=?, created_at=? WHERE id=?", (start.strftime("%Y-%m-%d %H:%M"), "2020-01-01 10:00", b["id"]))
    await reminders.send_reminders()
    assert any("Напоминаю: завтра" in t for t in fake.texts_to(MAX_CLIENT))

    # 9. Мастер подключается по приглашению в MAX и получает ссылку входа в кабинет
    m = db.one("SELECT * FROM masters WHERE name='Аня'")
    await feed(max_update("bot_started", chat_id=2, user=user(MAX_MASTER, "Аня"), payload=f"staff_{m['invite']}"))
    assert db.get_master(m["id"])["tg_id"] == -MAX_MASTER
    home = [b for k, w, b in fake.sent if k == "send" and w == MAX_MASTER][-1]
    cab = find(home, "кабинет")
    assert cab["type"] == "link" and "https://cab.example?k=" in cab["url"]
    token = cab["url"].split("k=")[1]
    me = c.get("/api/me", headers={"X-Login": token}).json()
    assert me["role"] == "master" and me["name"] == "Аня", me
    assert c.get("/api/me", headers={"X-Login": "wrong"}).status_code == 401
    print("✅ Мастер из MAX вошёл в кабинет по личной ссылке")
    print("✅ MAX работает так же, как Telegram")


asyncio.run(main())
