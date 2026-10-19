"""Проверка всего сценария салона без реального Telegram: python -m tests.test_flow"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="111", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Contact, Message, PhotoSize, Update, User
from fastapi.testclient import TestClient

from app import api, bot as bot_module, config, db, notify, reminders, slots

OWNER, ANYA_TG, CLIENT, CLIENT2 = 111, 444, 222, 333


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


def last_kb(session, chat):
    for c, _, _, kb in reversed(session.sent):
        if c == chat and kb is not None and hasattr(kb, "inline_keyboard"):
            return kb.inline_keyboard
    raise AssertionError("нет клавиатуры")


def find_btn(rows, text):
    for r in rows:
        for b in r:
            if text in b.text:
                return b
    raise AssertionError(f"нет кнопки «{text}»: {[b.text for r in rows for b in r]}")


def first_cb(rows, prefix):
    for r in rows:
        for b in r:
            if b.callback_data and b.callback_data.startswith(prefix):
                return b
    raise AssertionError(f"нет кнопки {prefix}")


def sent_to(session, chat, needle):
    return any(c == chat and needle in t for c, _, t, _ in session.sent)


async def main():
    db.init()
    session = FakeSession()
    bot = Bot(config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    config.runtime.update(bot_username="lunka_test_bot", webapp_url="https://example.trycloudflare.com")
    notify.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(bot_module.router)
    c = TestClient(api.app)
    H = {"X-Init-Data": "dev"}

    # 1. Владелец подключает салон
    await dp.feed_update(bot, msg(OWNER, "/start"))
    await dp.feed_update(bot, cb(OWNER, "reg"))
    assert db.is_admin(OWNER)
    assert len(c.get("/api/services", headers=H).json()) == 7, "нет стартового прайса"
    assert c.get("/api/me", headers={"X-Init-Data": "bad"}).status_code == 401

    # 2. Добавляет двух мастеров; Катя не делает наращивание
    anya = c.post("/api/masters", headers=H, json={"name": "Аня", "spec": "Топ-мастер"}).json()
    katya = c.post("/api/masters", headers=H, json={"name": "Катя"}).json()
    svcs = {s["name"]: s for s in c.get("/api/services", headers=H).json()}
    gel, ext = svcs["Маникюр + покрытие гель-лаком"], svcs["Наращивание ногтей"]
    week = [{"weekday": i, "works": True, "start": "10:00", "end": "20:00"} for i in range(7)]
    for m in (anya, katya):
        ids = [s["id"] for s in svcs.values() if not (m is katya and s["id"] == ext["id"])]
        assert c.put(f"/api/masters/{m['id']}", headers=H, json={"service_ids": ids, "schedule": week}).status_code == 200

    # 3. Аня подключается по приглашению
    invite = anya["invite_link"].split("start=")[1]
    await dp.feed_update(bot, msg(ANYA_TG, f"/start {invite}"))
    assert db.get_master(anya["id"])["tg_id"] == ANYA_TG
    assert sent_to(session, OWNER, "подключил")

    # 4. Клиент: услуга → «любой мастер» → день → время → телефон
    await dp.feed_update(bot, msg(CLIENT, "/start"))
    await dp.feed_update(bot, cb(CLIENT, find_btn(last_kb(session, CLIENT), "Записаться").callback_data))
    await dp.feed_update(bot, cb(CLIENT, find_btn(last_kb(session, CLIENT), "гель-лаком").callback_data))
    rows = last_kb(session, CLIENT)
    find_btn(rows, "Аня"); find_btn(rows, "Катя")
    await dp.feed_update(bot, cb(CLIENT, find_btn(rows, "Любой").callback_data))
    assert "Шаг 3 из 4" in session.sent[-1][2], session.sent[-1][2]
    days_rows = last_kb(session, CLIENT)
    assert any("В начало" in b.text for r in days_rows for b in r), "нет кнопки «В начало»"
    await dp.feed_update(bot, cb(CLIENT, days_rows[0][1].callback_data))
    trows = last_kb(session, CLIENT)
    assert trows[0][0].text in ("☀️ Утро", "🌤 День", "🌙 Вечер"), trows[0][0].text
    await dp.feed_update(bot, cb(CLIENT, first_cb(trows, "t:").callback_data))
    # на шаге телефона нижнее меню продолжает работать: «Как записаться» не ломает запись
    await dp.feed_update(bot, msg(CLIENT, bot_module.B_HELP))
    assert "Как записаться" in session.sent[-1][2]
    await dp.feed_update(bot, msg(CLIENT, bot_module.B_BOOK))
    await dp.feed_update(bot, cb(CLIENT, find_btn(last_kb(session, CLIENT), "гель-лаком").callback_data))
    await dp.feed_update(bot, cb(CLIENT, find_btn(last_kb(session, CLIENT), "Любой").callback_data))
    await dp.feed_update(bot, cb(CLIENT, last_kb(session, CLIENT)[0][1].callback_data))
    await dp.feed_update(bot, cb(CLIENT, first_cb(last_kb(session, CLIENT), "t:").callback_data))
    await dp.feed_update(bot, msg(CLIENT, "не номер"))
    assert "Не получилось распознать номер" in session.sent[-1][2]
    await dp.feed_update(bot, msg(CLIENT, contact=Contact(phone_number="79001234567", first_name="Анна", user_id=CLIENT)))
    assert any("Вы записаны" in t for _, _, t, _ in session.sent)
    b = db.one("SELECT * FROM bookings")
    print("Запись:", b["start"], b["service_name"], "→ мастер", db.get_master(b["master_id"])["name"])
    assert sent_to(session, OWNER, "Новая запись")

    # 5. Второй клиент на то же время «к любому» попадает ко второму мастеру
    d = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
    other = slots.pick_master(gel, d.date(), b["start"][11:], 0)
    assert other and other != b["master_id"], "свободный мастер не найден"
    # а у занятого мастера это время пропало
    assert b["start"][11:] not in slots.free_slots(b["master_id"], d.date(), gel["duration"])

    # 6. На наращивание Катю не предлагают — единственный мастер выбирается сам
    session.sent.clear()
    await dp.feed_update(bot, cb(CLIENT2, bot_module.Svc(s=ext["id"]).pack()))
    assert "Мастер: Аня" in session.sent[-1][2] and "Шаг 2 из 3" in session.sent[-1][2], session.sent[-1][2]

    # 6б. Клиент пишет вопрос текстом — уходит владельцу, клиенту показывается меню
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, "Можно прийти с ребёнком?"))
    assert sent_to(session, OWNER, "Можно прийти с ребёнком?")
    assert "передал" in session.sent[-1][2]

    # 6б-2. Владелец нажимает «Ответить» → режим ответа → клиентка получает сообщение от салона
    cid = db.find_client(CLIENT)["id"]
    session.sent.clear()
    await dp.feed_update(bot, cb(OWNER, notify.Reply(c=cid).pack()))
    assert "Режим ответа" in session.sent[-1][2] and "ребёнком" in session.sent[-1][2]
    await dp.feed_update(bot, msg(OWNER, "Конечно, приходите!"))
    assert sent_to(session, CLIENT, "Конечно, приходите!") and sent_to(session, OWNER, "Отправлено")
    await dp.feed_update(bot, msg(OWNER, "✖️ Закончить ответ"))
    assert "выключен" in session.sent[-1][2]
    # во время живого разговора клиентке не повторяется автоответ
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, "Спасибо!"))
    assert not sent_to(session, CLIENT, "передал"), "автоответ повторился"
    # ответ свайпом на пересланное сообщение
    relay = db.one("SELECT * FROM relay WHERE chat_id=? ORDER BY message_id DESC", (OWNER,))
    session.sent.clear()
    await dp.feed_update(bot, Update(update_id=3, message=Message(
        message_id=50, date=datetime.now(), chat=Chat(id=OWNER, type="private"), from_user=user(OWNER), text="Ждём вас 🌸",
        reply_to_message=Message(message_id=relay["message_id"], date=datetime.now(), chat=Chat(id=OWNER, type="private"), text="x"))))
    assert sent_to(session, CLIENT, "Ждём вас")
    # кабинет: список чатов и ответ из кабинета
    chats = c.get("/api/chats", headers=H).json()
    assert chats and chats[0]["id"] == cid, chats
    th = c.get(f"/api/chats/{cid}", headers=H).json()
    assert [m["direction"] for m in th["messages"]][:3] == ["in", "out", "in"], th["messages"]
    session.sent.clear()
    assert c.post(f"/api/chats/{cid}", headers=H, json={"text": "Из кабинета"}).status_code == 200
    assert sent_to(session, CLIENT, "Из кабинета")
    # приглашение директора
    link = c.get("/api/admin_invite", headers=H).json()["link"]
    await dp.feed_update(bot, msg(555, "/start " + link.split("start=")[1]))
    assert db.is_admin(555)

    # 6в. Перенос записи: новая создаётся, старая отменяется
    old_id = db.one("SELECT id FROM bookings WHERE status='active'")["id"]
    session.sent.clear()
    await dp.feed_update(bot, cb(CLIENT, bot_module.Move(b=old_id).pack()))
    await dp.feed_update(bot, cb(CLIENT, last_kb(session, CLIENT)[1][0].callback_data))
    await dp.feed_update(bot, cb(CLIENT, first_cb(last_kb(session, CLIENT), "t:").callback_data))
    await dp.feed_update(bot, cb(CLIENT, first_cb(last_kb(session, CLIENT), "o:").callback_data))
    assert any("Запись перенесена" in t for _, _, t, _ in session.sent), [t for _, _, t, _ in session.sent][-3:]
    assert db.get_booking(old_id)["status"] == "cancelled"
    b = db.one("SELECT * FROM bookings WHERE status='active'")
    d = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")

    # 7. Фото дизайна уходит мастеру записи и владельцу
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]))
    assert "Фото передано" in session.sent[-1][2]

    # 8. Кабинет: записи по мастеру, удаление мастера с записями запрещено
    day = d.date().isoformat()
    assert len(c.get(f"/api/bookings?day={day}", headers=H).json()["bookings"]) == 1
    assert len(c.get(f"/api/bookings?day={day}&master_id={b['master_id']}", headers=H).json()["bookings"]) == 1
    assert c.delete(f"/api/masters/{b['master_id']}", headers=H).status_code == 409
    r = c.post("/api/bookings", headers=H, json={"client_name": "Ольга", "service_id": gel["id"],
               "master_id": b["master_id"], "day": day, "time": b["start"][11:]})
    assert r.status_code == 409, r.text

    # 9. Мастер видит только свои записи и не может управлять мастерами
    orig = api.safe_parse_webapp_init_data
    api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=ANYA_TG, first_name="Аня", last_name=None))
    HM = {"X-Init-Data": "anya"}
    me = c.get("/api/me", headers=HM).json()
    assert me["role"] == "master" and me["master_id"] == anya["id"]
    assert c.post("/api/masters", headers=HM, json={"name": "X"}).status_code == 403
    assert c.get("/api/chats", headers=HM).status_code == 403
    assert c.put(f"/api/masters/{katya['id']}", headers=HM, json={"schedule": week}).status_code == 403
    own = c.get(f"/api/bookings?day={day}&master_id={katya['id']}", headers=HM).json()["bookings"]
    assert all(x["master_id"] == anya["id"] for x in own)
    api.safe_parse_webapp_init_data = orig

    # 10. Напоминание за сутки, без повторов
    start = db.now_local() + timedelta(hours=23)
    db.ex("UPDATE bookings SET start=?, created_at=? WHERE id=?", (start.strftime("%Y-%m-%d %H:%M"), "2020-01-01 10:00", b["id"]))
    session.sent.clear()
    await reminders.send_reminders()
    assert sent_to(session, CLIENT, "Напоминаю: завтра")
    session.sent.clear()
    await reminders.send_reminders()
    assert not session.sent

    # 11. Клиент отменяет → мастер и владелец узнают; мастера без записей можно удалить
    await dp.feed_update(bot, cb(CLIENT, notify.CCancel(b=b["id"], sure=1).pack()))
    assert db.get_booking(b["id"])["status"] == "cancelled"
    assert sent_to(session, OWNER, "отменил")
    assert c.delete(f"/api/masters/{katya['id']}", headers=H).status_code == 200
    assert len(c.get("/api/masters", headers=H).json()) == 1

    assert "Кабинет салона" in c.get("/").text
    print("✅ Все проверки пройдены")


asyncio.run(main())
