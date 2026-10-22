"""Отзывы: просьба после визита → звёзды → текст → фото → ответ мастера → видно клиентам и в кабинете.
python -m tests.test_reviews"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="111", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, File, Message, PhotoSize, Update, User
from fastapi.testclient import TestClient

from app import api, bot as bot_module, config, db, notify, reviews

OWNER, MASTER_TG, CLIENT = 111, 444, 222


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


class Session(FakeSession):
    async def make_request(self, bot, method, timeout=None):
        if type(method).__name__ == "GetFile":
            return File(file_id=method.file_id, file_unique_id="u", file_path="photos/p.jpg")
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, *a, **k):
        yield JPEG


async def main():
    db.init()
    config.runtime["webapp_url"] = "https://zarina.example.ru"
    session = Session()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    notify.bot = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(reviews.router)
    dp.include_router(bot_module.router)

    db.add_admin(OWNER, "Зарина")
    m = db.create_master("Аня", "", MASTER_TG)
    svc = db.q("SELECT * FROM services")[0]
    c = db.get_or_create_client(CLIENT, "Анна Петрова", "+79001112233", "anna")
    now = db.now_local()
    y = (now - timedelta(days=1)).replace(hour=10, minute=0)
    start = y.strftime("%Y-%m-%d %H:%M")
    bid = db.create_booking(m["id"], c["id"], svc, start)
    future = db.create_booking(m["id"], c["id"], svc, (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))

    # 1. Просьба об отзыве — только о прошедшем визите, один раз, днём
    day = y.replace(hour=16)
    due = [b["id"] for b in reviews.due_requests(day)]
    assert due == [bid] and future not in due, due
    assert reviews.due_requests(y.replace(hour=23)) == [], "ночью не беспокоим"
    assert reviews.due_requests(y.replace(hour=11)) == [], "визит ещё не закончился"
    import app.reviews as rv
    orig = db.now_local
    db.now_local = lambda: day
    await rv.send_requests()
    await rv.send_requests()
    db.now_local = orig
    asks = [s for s in session.sent if s[0] == CLIENT and "Как вам визит" in s[2]]
    assert len(asks) == 1, "просьба должна прийти ровно один раз"

    # 2. Чужая запись — нельзя оценить
    await dp.feed_update(bot, cb(999, reviews.Rate(b=bid, r=5).pack()))
    assert not db.q("SELECT 1 FROM reviews")

    # 3. Клиент: 5 звёзд → текст → фото
    await dp.feed_update(bot, cb(CLIENT, reviews.Rate(b=bid, r=5).pack()))
    await dp.feed_update(bot, msg(CLIENT, "Аня — волшебница, всё аккуратно!"))
    photo = [PhotoSize(file_id="F1", file_unique_id="u1", width=10, height=10)]
    await dp.feed_update(bot, msg(CLIENT, photo=photo))
    r = db.one("SELECT * FROM reviews")
    assert r["rating"] == 5 and "волшебница" in r["text"] and r["photo"].endswith(".jpg"), r
    assert (db.REVIEWS_DIR / r["photo"]).read_bytes() == JPEG
    staff = [s for s in session.sent if "Новый отзыв" in s[2]]
    assert {s[0] for s in staff} == {MASTER_TG, OWNER}, staff
    assert "Анна" in staff[0][2] and "Петрова" not in staff[0][2], "в отзыве только имя"
    # текст отзыва не ушёл администратору как обычное сообщение клиента
    assert not [s for s in session.sent if s[0] == OWNER and "волшебница" in s[2] and "Новый отзыв" not in s[2]]

    # 4. Мастер отвечает в боте → клиент получает ответ
    await dp.feed_update(bot, cb(MASTER_TG, reviews.ReplyTo(v=r["id"]).pack()))
    await dp.feed_update(bot, msg(MASTER_TG, "Спасибо, Анна! Ждём снова 🌸"))
    r = db.get_review(r["id"])
    assert r["reply"].startswith("Спасибо") and r["reply_by"] == "Аня"
    assert [s for s in session.sent if s[0] == CLIENT and "ответил(а) на ваш отзыв" in s[2]]
    # чужой мастер ответить не может
    other = db.create_master("Оля", "", 555)
    await dp.feed_update(bot, cb(555, reviews.ReplyTo(v=r["id"]).pack()))
    await dp.feed_update(bot, msg(555, "взлом"))
    assert db.get_review(r["id"])["reply"].startswith("Спасибо")

    # 5. Клиент видит рейтинг и отзывы
    assert db.master_ratings()[m["id"]] == (5.0, 1)
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, bot_module.B_REVIEWS))
    assert any("Аня · ★ 5,0 (1)" in str(s[3]) for s in session.sent), session.sent
    await dp.feed_update(bot, cb(CLIENT, reviews.ShowRev(m=m["id"]).pack()))
    shown = session.sent[-1][2]
    assert "волшебница" in shown and "https://zarina.example.ru/r/" in shown and "Ждём снова" in shown, shown

    # 6. Кабинет: владелец видит, отвечает, скрывает; мастер — только свои; фото по ссылке
    tc = TestClient(api.app)
    H = {"X-Init-Data": "dev"}
    data = tc.get("/api/reviews", headers=H).json()
    assert data["reviews"][0]["client"] == "Анна" and data["ratings"][str(m["id"])]["n"] == 1
    assert tc.get("/" + data["reviews"][0]["photo"]).content == JPEG
    assert tc.get("/r/../t.db").status_code == 404
    assert tc.post(f"/api/reviews/{r['id']}/reply", headers=H, json={"text": "Ответ салона"}).status_code == 200
    assert db.get_review(r["id"])["reply_by"] == "Салон"
    assert tc.post(f"/api/reviews/{r['id']}/hide", headers=H).json()["hidden"] is True
    assert db.master_ratings() == {}, "скрытый отзыв не влияет на рейтинг"
    assert tc.get("/" + data["reviews"][0]["photo"]).status_code == 404, "фото скрытого отзыва недоступно"
    tc.post(f"/api/reviews/{r['id']}/hide", headers=H)

    # 7. Клиент без бота (записан вручную): ссылка → страница → отзыв с фото
    import base64
    walkin = db.get_or_create_client(None, "Елена Смирнова", "+79005556677")
    wb = db.create_booking(m["id"], walkin["id"], svc, start)
    link = tc.get(f"/api/bookings/{wb}/review_link", headers=H).json()
    assert link["link"].startswith("https://zarina.example.ru/review.html?t=") and "Елена" in link["text"]
    token = link["link"].split("t=")[1]
    info = tc.get(f"/api/public/review/{token}").json()
    assert info["master"] == "Аня" and info["started"] is True and info["review"] is None
    assert tc.get("/api/public/review/nope").status_code == 404
    assert tc.post(f"/api/public/review/{token}", json={"rating": 4, "text": "x", "photo": "data:image/png;base64,iVBORw0KGgo="}).status_code == 400
    session.sent.clear()
    ok = tc.post(f"/api/public/review/{token}", json={"rating": 4, "text": "Очень уютно",
                                                       "photo": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()})
    assert ok.status_code == 200, ok.text
    wr = db.one("SELECT * FROM reviews WHERE booking_id=?", (wb,))
    assert wr["rating"] == 4 and wr["author"] == "Елена" and (db.REVIEWS_DIR / wr["photo"]).read_bytes() == JPEG
    assert [s_ for s_ in session.sent if "Новый отзыв" in s_[2]], "мастер узнал об отзыве по ссылке"
    # будущий визит — по ссылке ещё нельзя
    fb = db.create_booking(m["id"], walkin["id"], svc, (now + timedelta(days=2)).strftime("%Y-%m-%d %H:%M"))
    ft = tc.get(f"/api/bookings/{fb}/review_link", headers=H).json()["link"].split("t=")[1]
    assert tc.post(f"/api/public/review/{ft}", json={"rating": 5}).status_code == 409
    # мастер не может взять ссылку на чужую запись
    api_parse = api.safe_parse_webapp_init_data
    from types import SimpleNamespace
    api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=555, first_name="Оля", last_name=None))
    assert tc.get(f"/api/bookings/{wb}/review_link", headers={"X-Init-Data": "x"}).status_code == 404
    api.safe_parse_webapp_init_data = api_parse

    # 8. Клиент с ботом сам пишет отзыв о визите, о котором его ещё не спрашивали
    later = db.create_booking(m["id"], c["id"], svc, (y - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))
    assert [v["id"] for v in db.reviewable_visits(CLIENT)] == [later]
    session.sent.clear()
    await dp.feed_update(bot, msg(CLIENT, bot_module.B_REVIEWS))
    assert any("Оставить отзыв" in str(s_[3]) for s_ in session.sent)
    await dp.feed_update(bot, cb(CLIENT, "rv_write"))
    await dp.feed_update(bot, cb(CLIENT, reviews.Rate(b=later, r=3).pack()))
    await dp.feed_update(bot, msg(CLIENT, "Неплохо"))
    await dp.feed_update(bot, cb(CLIENT, "rv_done"))
    assert db.one("SELECT rating, text FROM reviews WHERE booking_id=?", (later,)) == {"rating": 3, "text": "Неплохо"}

    # 9. Удаление клиента: телефон и переписка стираются, ОТЗЫВЫ ОСТАЮТСЯ (с именем и фото)
    n_before = db.one("SELECT COUNT(*) n FROM reviews WHERE client_id=?", (c["id"],))["n"]
    photo_file = db.REVIEWS_DIR / r["photo"]
    db.cancel_booking(future, "salon")
    db.delete_client(c["id"])
    kept = db.q(db.REVIEW_SQL + " WHERE r.client_id=?", (c["id"],))
    assert len(kept) == n_before and photo_file.exists()
    assert all(db.review_author(x) == "Анна" for x in kept), [db.review_author(x) for x in kept]
    assert db.one("SELECT phone FROM clients WHERE id=?", (c["id"],))["phone"] == ""
    # 10. Перенос отзывов с прошлых площадок (с фото работы), с честной пометкой
    import base64 as _b64
    body = {"master_id": m["id"], "author": "Ольга К.", "rating": 5, "text": "Лучший мастер в городе!",
            "source": "Яндекс Карты", "day": "2026-05-14",
            "photo": "data:image/jpeg;base64," + _b64.b64encode(JPEG).decode()}
    res = tc.post("/api/reviews", headers=H, json=body)
    assert res.status_code == 200, res.text
    imp = db.get_review(res.json()["id"])
    assert imp["source"] == "Яндекс Карты" and db.review_author(imp) == "Ольга К." and imp["booking_id"] is None
    assert (db.REVIEWS_DIR / imp["photo"]).read_bytes() == JPEG
    listed = [x for x in tc.get("/api/reviews", headers=H).json()["reviews"] if x["id"] == imp["id"]][0]
    assert listed["source"] == "Яндекс Карты" and listed["date"] == "2026-05-14"
    assert "перенесён: Яндекс Карты" in reviews.review_block(imp)
    assert tc.post("/api/reviews", headers=H, json={**body, "day": "2999-01-01"}).status_code == 400, "дата из будущего"
    assert tc.post("/api/reviews", headers=H, json={**body, "text": "", "photo": ""}).status_code == 400, "пустой отзыв"
    # настоящий отзыв клиента удалить нельзя, перенесённый — можно
    real = db.one("SELECT id FROM reviews WHERE source='' LIMIT 1")["id"]
    assert tc.delete(f"/api/reviews/{real}", headers=H).status_code == 403
    assert tc.delete(f"/api/reviews/{imp['id']}", headers=H).status_code == 200
    assert not db.get_review(imp["id"]) and not (db.REVIEWS_DIR / imp["photo"]).exists()

    print("✅ Отзывы: бот, ссылка для клиентов без бота, фото, ответ мастера, рейтинг, кабинет — всё работает; при удалении клиента отзывы остаются")


asyncio.run(main())
