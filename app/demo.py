"""Демо-салон (DEMO=1 в tenant.env): будущие заказчики сами пробуют сервис.

• Посетитель выбирает, кем попробовать: клиентом, владельцем или мастером (/demo — сменить роль).
• Настоящих сотрудников нет: то, что получили бы мастер и владелец, бот показывает самому посетителю.
• Сообщения «другим людям» (ответ в чате, отмена записи салоном…) уходят тому, кто нажал, — посетители
  не могут писать друг другу от имени салона. Телефоны в кабинете скрыты.
• Каждую ночь в 04:00 салон возвращается к эталону (снимок базы), записи на неделю создаются заново.
"""
import logging
import random
import sqlite3
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, MenuButtonDefault, Message, ReplyKeyboardRemove

from . import config, db, notify

log = logging.getLogger("demo")
router = Router()
SNAPSHOT = config.TENANT_DIR / "demo_snapshot.db"
FLAG_RESET = config.TENANT_DIR / "demo_reset.flag"
FLAG_SNAPSHOT = config.TENANT_DIR / "demo_snapshot.flag"
RESET_HOUR = 4
_state = {"last_reset": ""}

FAKE_CLIENTS = ["Алина", "Екатерина", "Ольга", "Дарья", "Ксения", "Виктория", "Светлана", "Юлия", "Марина", "Полина"]
MASTERS = [("Анна", "Топ-мастер"), ("Мария", "Мастер"), ("Ольга", "Мастер, стаж 5 лет")]
REVIEWS = [(5, "Всё очень понравилось, аккуратно и быстро. Обязательно приду ещё!"),
           (5, "Лучший мастер, хожу уже год 💖"),
           (4, "Хорошо, но пришлось немного подождать."),
           (5, "Записалась за минуту прямо в Telegram, очень удобно."),
           (5, "Спасибо за красоту! Держится отлично."),
           (5, "Уютно, чисто, приветливо. Рекомендую!")]
FALLBACK_SERVICES = [("Консультация", 1000, 30), ("Основная услуга", 2000, 60), ("Комплекс", 3500, 120)]


def contact_row() -> list:
    return [[InlineKeyboardButton(text="💬 Хочу такую же запись", url=f"https://t.me/{config.LOG_OWNER}")]]


# ======================================================================
#  Выбор роли
# ======================================================================
def picker_kb():
    from .bot import btn, kb
    return kb([[btn("👤 Как клиент — записаться", "demo:client")],
               [btn("👑 Как владелец — кабинет салона", "demo:owner")],
               [btn("✂️ Как мастер — что видит мастер", "demo:master")]] + contact_row())


async def show_picker(message: Message):
    name = db.settings().get("salon_name", "")
    await message.answer(
        f"👋 Это <b>демо онлайн-записи</b> «{name}».\n\n"
        "Попробуйте всё сами — ничего не сломаете 🙂\n\n"
        "👤 <b>Клиент</b> — запишитесь, перенесите, отмените, напишите салону\n"
        "👑 <b>Владелец</b> — кабинет: записи, мастера, клиенты, отзывы, выручка\n"
        "✂️ <b>Мастер</b> — свои записи, график и клиенты\n\n"
        "Сменить роль в любой момент — команда /demo\n\n"
        "<i>Это общая демо-площадка: данные видят другие посетители и стираются каждую ночь, "
        "телефоны скрыты.</i>",
        reply_markup=picker_kb())


@router.message(Command("demo"))
async def demo_cmd(message: Message):
    if config.DEMO:
        await show_picker(message)


@router.callback_query(F.data.startswith("demo:"))
async def pick(cb: CallbackQuery):
    if not config.DEMO:
        await cb.answer()
        return
    from . import bot as b
    role = cb.data.split(":", 1)[1]
    if role not in ("client", "owner", "master"):
        await cb.answer()
        return
    uid = cb.from_user.id
    db.set_demo_role(uid, role)
    await cb.answer()
    if role == "owner":
        await cb.message.answer("👑 Вы — владелец демо-салона. Откройте кабинет 👇", reply_markup=ReplyKeyboardRemove())
        await b.show_admin_home(cb.message, uid)
    elif role == "master":
        m = db.master_by_tg(uid)
        if not m:
            await cb.message.answer("В демо-салоне пока нет мастеров.")
            return
        await cb.message.answer(f"✂️ Вы смотрите глазами мастера {m['name']}.", reply_markup=ReplyKeyboardRemove())
        await b.show_master_home(cb.message, m)
    else:
        try:
            await cb.bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonDefault())
        except Exception:
            pass
        await b.client_welcome(cb.message)


async def actor_mw(handler, event, data):
    """Запоминаем, кто нажал, — чтобы в демо сообщения «другим» уходили ему самому."""
    user = data.get("event_from_user")
    token = notify.ACTOR.set(user.id if user else None)
    try:
        return await handler(event, data)
    finally:
        notify.ACTOR.reset(token)


# ======================================================================
#  Данные демо-салона
# ======================================================================
def seed_static():
    """Мастера, услуги, отзывы и описание — если салон пустой."""
    if not db.services():
        for name, price, dur in FALLBACK_SERVICES:
            db.ex("INSERT INTO services (name, price, duration) VALUES (?,?,?)", (name, price, dur))
    if not db.masters():
        for name, spec in MASTERS:
            db.create_master(name, spec)
    s = db.settings()
    if not s.get("address"):
        db.set_setting("address", "ул. Примерная, 1 (демо)")
    if not s.get("phone"):
        db.set_setting("phone", "+7 900 000-00-00")
    if not s.get("about"):
        db.set_setting("about", "Это демо-салон: запишитесь и посмотрите, как всё работает. Ни к чему не обязывает 🙂")
    if not db.q("SELECT 1 FROM reviews LIMIT 1"):
        ms, svs = db.masters(), db.services()
        for i, (rating, text) in enumerate(REVIEWS):
            m, sv = ms[i % len(ms)], svs[i % len(svs)]
            day = (db.now_local() - timedelta(days=3 + i * 4)).replace(hour=12, minute=0)
            cid = _fake_client(i + 3)
            bid = db.ex("INSERT INTO bookings (master_id, client_id, service_id, service_name, price, start, duration, "
                        "review_asked, created_at) VALUES (?,?,?,?,?,?,?,1,?)",
                        (m["id"], cid, sv["id"], sv["name"], sv["price"], day.strftime("%Y-%m-%d %H:%M"),
                         sv["duration"], db.now_str()))
            db.ex("INSERT INTO reviews (booking_id, master_id, client_id, rating, author, text, created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (bid, m["id"], cid, rating, FAKE_CLIENTS[(i + 3) % 10], text,
                                             (day + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")))


def _fake_client(i: int) -> int:
    name = FAKE_CLIENTS[i % len(FAKE_CLIENTS)]
    phone = f"+7 900 000-00-{i % 100:02d}"
    c = db.one("SELECT id FROM clients WHERE tg_id IS NULL AND phone=?", (phone,))
    if c:
        return c["id"]
    return db.ex("INSERT INTO clients (tg_id, name, phone, created_at) VALUES (NULL,?,?,?)", (name, phone, db.now_str()))


def seed_bookings(days: int = 7):
    """Записи «вымышленных» клиенток на ближайшую неделю — чтобы кабинет не был пустым."""
    rnd = random.Random()
    now = db.now_local()
    for d in range(days):
        day = (now + timedelta(days=d)).date()
        for m in db.masters():
            sch = db.one('SELECT start, "end" FROM schedule WHERE master_id=? AND weekday=?', (m["id"], day.weekday()))
            if not sch or db.one("SELECT 1 FROM days_off WHERE master_id=? AND day=?", (m["id"], day.isoformat())):
                continue
            sids = db.master_service_ids(m["id"])
            if not sids:
                continue
            h0, h1 = int(sch["start"][:2]), int(sch["end"][:2])
            hours = list(range(h0 + 1, h1 - 1, 2))
            for hour in rnd.sample(hours, k=min(2, len(hours))):
                start = datetime.combine(day, datetime.min.time()).replace(hour=hour)
                if start <= now:
                    continue
                sv = db.one("SELECT * FROM services WHERE id=?", (rnd.choice(sids),))
                if not sv:
                    continue
                db.ex("INSERT INTO bookings (master_id, client_id, service_id, service_name, price, start, duration, "
                      "remind24, remind2, review_asked, created_at) VALUES (?,?,?,?,?,?,?,1,1,1,?)",
                      (m["id"], _fake_client(rnd.randrange(10)), sv["id"], sv["name"], sv["price"],
                       start.strftime("%Y-%m-%d %H:%M"), sv["duration"], db.now_str()))


def save_snapshot():
    dst = sqlite3.connect(SNAPSHOT)
    try:
        db._conn.backup(dst)
    finally:
        dst.close()
    log.info("Демо: эталон сохранён")


def reset():
    """Вернуть демо к эталону: всё, что натворили посетители, стирается; роли посетителей остаются."""
    roles = db.q("SELECT * FROM demo_roles")
    if SNAPSHOT.exists():
        src = sqlite3.connect(SNAPSHOT)
        try:
            src.backup(db._conn)
        finally:
            src.close()
    now = db.now_str()
    for sql in ("DELETE FROM messages", "DELETE FROM relay", "DELETE FROM logins",
                "DELETE FROM reviews WHERE client_id IN (SELECT id FROM clients WHERE tg_id IS NOT NULL)",
                "DELETE FROM bookings WHERE start>=? OR client_id IN (SELECT id FROM clients WHERE tg_id IS NOT NULL)",
                "DELETE FROM clients WHERE tg_id IS NOT NULL", "DELETE FROM admins",
                "UPDATE masters SET tg_id=NULL"):
        db.ex(sql, (now,) if "?" in sql else ())
    for r in roles:
        db.set_demo_role(r["tg_id"], r["role"])
    seed_static()
    seed_bookings()
    log.info("Демо-салон сброшен к эталону")


def ensure_ready():
    """При запуске: заполнить пустой демо-салон и запомнить его как эталон."""
    if not config.DEMO:
        return
    seed_static()
    if not SNAPSHOT.exists():
        db.ex("DELETE FROM bookings WHERE start>=?", (db.now_str(),))
        save_snapshot()
    if not db.one("SELECT 1 FROM bookings WHERE start>=?", (db.now_str(),)):
        seed_bookings()


def tick():
    """Раз в минуту (из reminders.loop): ночной сброс и команды из бота техподдержки."""
    if not config.DEMO:
        return
    if FLAG_SNAPSHOT.exists():
        FLAG_SNAPSHOT.unlink(missing_ok=True)
        db.ex("DELETE FROM bookings WHERE start>=?", (db.now_str(),))  # в эталон — без будущих записей
        save_snapshot()
        seed_bookings()
    now = db.now_local()
    today = now.date().isoformat()
    if FLAG_RESET.exists() or (now.hour == RESET_HOUR and _state["last_reset"] != today):
        FLAG_RESET.unlink(missing_ok=True)
        _state["last_reset"] = today
        reset()
