"""База данных ОДНОГО салона: файл tenants/<салон>/lunka.db. Салоны друг друга не видят."""
import os
import secrets
import sqlite3
import threading
from datetime import datetime

from .config import DB_PATH, SHARED_DIR, TZ
from .niche import N

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (               -- владелец/администратор салона
    tg_id INTEGER PRIMARY KEY,
    name  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS masters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    spec        TEXT DEFAULT '',                 -- «Топ-мастер, наращивание»
    tg_id       INTEGER,                         -- заполняется, когда мастер подключил Telegram
    invite      TEXT NOT NULL,                   -- код ссылки-приглашения
    active      INTEGER NOT NULL DEFAULT 1,
    last_digest TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    price      INTEGER NOT NULL DEFAULT 0,
    duration   INTEGER NOT NULL DEFAULT 60,
    price_from INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS master_services (      -- какие услуги делает мастер
    master_id  INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    PRIMARY KEY (master_id, service_id)
);
CREATE TABLE IF NOT EXISTS schedule (
    master_id INTEGER NOT NULL,
    weekday   INTEGER NOT NULL,                  -- 0 = понедельник
    start     TEXT NOT NULL,
    "end"     TEXT NOT NULL,
    PRIMARY KEY (master_id, weekday)
);
CREATE TABLE IF NOT EXISTS days_off (
    master_id INTEGER NOT NULL,
    day       TEXT NOT NULL,
    PRIMARY KEY (master_id, day)
);
CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER UNIQUE,
    username   TEXT DEFAULT '',
    name       TEXT NOT NULL,
    phone      TEXT DEFAULT '',
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id    INTEGER NOT NULL,
    client_id    INTEGER NOT NULL,
    service_id   INTEGER,
    service_name TEXT NOT NULL,
    price        INTEGER NOT NULL DEFAULT 0,
    start        TEXT NOT NULL,                  -- 'YYYY-MM-DD HH:MM', местное время
    duration     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active', -- active | cancelled
    cancelled_by TEXT DEFAULT '',
    remind24     INTEGER NOT NULL DEFAULT 0,
    remind2      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_master_start ON bookings(master_id, start);
CREATE TABLE IF NOT EXISTS messages (           -- переписка салона с клиентами
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL,
    direction  TEXT NOT NULL,                   -- in (от клиента) | out (ответ салона)
    text       TEXT DEFAULT '',
    photo      TEXT DEFAULT '',                 -- file_id фото, если было
    author     TEXT DEFAULT '',                 -- кто из салона ответил
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_client ON messages(client_id, id);
CREATE TABLE IF NOT EXISTS logins (             -- личные ссылки входа в кабинет (для MAX и браузера)
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,                -- id сотрудника (Telegram > 0, MAX < 0)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relay (              -- какое уведомление у сотрудника относится к какому клиенту
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    client_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS reviews (            -- отзывы клиентов о мастерах (после визита)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER UNIQUE,                  -- один отзыв на визит
    master_id  INTEGER NOT NULL,
    client_id  INTEGER NOT NULL,
    rating     INTEGER NOT NULL,                -- 1..5
    author     TEXT DEFAULT '',                 -- имя автора (остаётся, даже если клиента удалят)
    text       TEXT DEFAULT '',
    photo      TEXT DEFAULT '',                 -- имя файла в tenants/<салон>/reviews/
    reply      TEXT DEFAULT '',                 -- ответ мастера / салона
    reply_by   TEXT DEFAULT '',
    reply_at   TEXT DEFAULT '',
    hidden     INTEGER NOT NULL DEFAULT 0,      -- владелец скрыл отзыв
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_master ON reviews(master_id, id);
"""

DEFAULT_SETTINGS = {
    "salon_name": os.getenv("NAME") or N["default_name"],  # NAME — из tenants/<салон>/tenant.env
    "address": "",
    "phone": "",
    "about": "",
    "slot_step": "30",
    "admin_digest": "",
    "admin_invite": "",
    "mode": "",          # solo — мастер работает сам на себя; salon (или пусто) — салон с мастерами
}

DEFAULT_SCHEDULE = {0: ("10:00", "20:00"), 1: ("10:00", "20:00"), 2: ("10:00", "20:00"),
                    3: ("10:00", "20:00"), 4: ("10:00", "20:00"), 5: ("10:00", "18:00")}

# Стартовый прайс зависит от типа салона (маникюр / парикмахерская) — см. niche.py
DEFAULT_SERVICES = N["services"]


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def now_str() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M")


def init(path=None):
    global _conn
    _conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    for sql in MIGRATIONS:  # новые колонки для баз из прошлых версий
        try:
            _conn.execute(sql)
            if "review_asked" in sql:  # колонка появилась только что: о старых визитах отзыв не просим
                _conn.execute("UPDATE bookings SET review_asked=1 WHERE start<?", (now_str(),))
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    for k, v in DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
    if not _conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
        for name, price, dur, pfrom in DEFAULT_SERVICES:
            _conn.execute("INSERT INTO services (name, price, duration, price_from) VALUES (?,?,?,?)",
                          (name, price, dur, pfrom))
    _conn.commit()


MIGRATIONS = [
    "ALTER TABLE clients ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN review_asked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN review_token TEXT DEFAULT ''",
    "ALTER TABLE reviews ADD COLUMN author TEXT DEFAULT ''",
]


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


# ---------- салон ----------
def settings() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def set_setting(key: str, value) -> None:
    ex("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))


def slot_step() -> int:
    return int(settings().get("slot_step", 30))


# ---------- режим разработчика ----------
# Аккаунт разработчика (сохраняет админ-бот в shared/admin.json) может войти в ЛЮБОЙ салон
# в роли клиента, хозяина или мастера. Режим салона хранится в settings: dev_mode =
# "client" | "owner" | "master:<id>" | "" (обычный). Другим людям это ни на что не влияет.
_dev = {"mtime": None, "id": 0}


def dev_id() -> int:
    f = SHARED_DIR / "admin.json"
    try:
        mt = f.stat().st_mtime
        if mt != _dev["mtime"]:
            import json
            _dev.update(mtime=mt, id=int(json.loads(f.read_text(encoding="utf-8")).get("dev_id") or 0))
    except Exception:
        _dev.update(mtime=None, id=0)
    return _dev["id"]


def dev_mode(tg_id: int) -> str | None:
    """Режим разработчика для этого человека; None — обычный пользователь или режим не выбран."""
    if not tg_id or tg_id != dev_id():
        return None
    return (one("SELECT value FROM settings WHERE key='dev_mode'") or {}).get("value") or None


def is_admin(tg_id: int) -> bool:
    mode = dev_mode(tg_id)
    if mode:
        return mode == "owner"
    return one("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,)) is not None


def admins() -> list[dict]:
    return q("SELECT * FROM admins")


def add_admin(tg_id: int, name: str):
    ex("INSERT OR IGNORE INTO admins (tg_id, name) VALUES (?,?)", (tg_id, name))


# ---------- мастера ----------
def masters(active_only=True) -> list[dict]:
    return q("SELECT * FROM masters" + (" WHERE active=1" if active_only else "") + " ORDER BY id")


def get_master(master_id: int) -> dict | None:
    return one("SELECT * FROM masters WHERE id=?", (master_id,))


def master_by_tg(tg_id: int) -> dict | None:
    mode = dev_mode(tg_id)
    if mode:
        if not mode.startswith("master:"):
            return None
        m = one("SELECT * FROM masters WHERE id=? AND active=1", (int(mode.split(":")[1]),))
        return {**m, "tg_id": tg_id} if m else None   # разработчик «в шкуре» этого мастера
    return one("SELECT * FROM masters WHERE tg_id=? AND active=1", (tg_id,))


def create_master(name: str, spec: str = "", tg_id: int | None = None) -> dict:
    code = secrets.token_hex(5)
    mid = ex("INSERT INTO masters (name, spec, tg_id, invite, created_at) VALUES (?,?,?,?,?)",
             (name, spec, tg_id, code, now_str()))
    for wd, (s, e) in DEFAULT_SCHEDULE.items():
        ex('INSERT INTO schedule (master_id, weekday, start, "end") VALUES (?,?,?,?)', (mid, wd, s, e))
    for s in services():
        ex("INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?,?)", (mid, s["id"]))
    return get_master(mid)


def master_service_ids(master_id: int) -> list[int]:
    return [r["service_id"] for r in q("SELECT service_id FROM master_services WHERE master_id=?", (master_id,))]


def masters_for_service(service_id: int) -> list[dict]:
    return q("""SELECT m.* FROM masters m JOIN master_services ms ON ms.master_id=m.id
                WHERE ms.service_id=? AND m.active=1 ORDER BY m.id""", (service_id,))


# ---------- услуги ----------
def services() -> list[dict]:
    return q("SELECT * FROM services WHERE active=1 ORDER BY id")


def get_service(service_id: int) -> dict | None:
    return one("SELECT * FROM services WHERE id=?", (service_id,))


def bookable_services() -> list[dict]:
    """Услуги, которые делает хотя бы один работающий мастер."""
    return q("""SELECT DISTINCT s.* FROM services s JOIN master_services ms ON ms.service_id=s.id
                JOIN masters m ON m.id=ms.master_id WHERE s.active=1 AND m.active=1 ORDER BY s.id""")


# ---------- график ----------
def schedule(master_id: int) -> dict[int, tuple[str, str]]:
    rows = q('SELECT weekday, start, "end" FROM schedule WHERE master_id=?', (master_id,))
    return {r["weekday"]: (r["start"], r["end"]) for r in rows}


def is_day_off(master_id: int, day: str) -> bool:
    return one("SELECT 1 FROM days_off WHERE master_id=? AND day=?", (master_id, day)) is not None


# ---------- клиенты ----------
def get_or_create_client(tg_id: int | None, name: str, phone: str = "", username: str = "") -> dict:
    if tg_id is not None:
        c = one("SELECT * FROM clients WHERE tg_id=?", (tg_id,))
        if c:
            if phone and phone != c["phone"]:
                ex("UPDATE clients SET phone=? WHERE id=?", (phone, c["id"]))
            if username and username != c["username"]:
                ex("UPDATE clients SET username=? WHERE id=?", (username, c["id"]))
            return one("SELECT * FROM clients WHERE id=?", (c["id"],))
    elif phone:
        c = one("SELECT * FROM clients WHERE phone=?", (phone,))
        if c:
            return c
    cid = ex("INSERT INTO clients (tg_id, username, name, phone, created_at) VALUES (?,?,?,?,?)",
             (tg_id, username, name, phone, now_str()))
    return one("SELECT * FROM clients WHERE id=?", (cid,))


def find_client(tg_id: int) -> dict | None:
    return one("SELECT * FROM clients WHERE tg_id=?", (tg_id,))


# ---------- записи ----------
def busy_intervals(master_id: int, day: str) -> list[tuple[str, int]]:
    rows = q("SELECT start, duration FROM bookings WHERE master_id=? AND status='active' AND start LIKE ?",
             (master_id, day + "%"))
    return [(r["start"][11:16], r["duration"]) for r in rows]


def create_booking(master_id: int, client_id: int, service: dict, start: str) -> int:
    return ex("""INSERT INTO bookings (master_id, client_id, service_id, service_name, price, start, duration, created_at)
                 VALUES (?,?,?,?,?,?,?,?)""",
              (master_id, client_id, service["id"], service["name"], service["price"], start,
               service["duration"], now_str()))


BOOKING_SQL = """SELECT b.*, c.name AS client_name, c.phone AS client_phone, c.tg_id AS client_tg,
                        c.username AS client_username, c.note AS client_note,
                        m.name AS master_name, m.tg_id AS master_tg
                 FROM bookings b JOIN clients c ON c.id=b.client_id JOIN masters m ON m.id=b.master_id"""


def get_booking(booking_id: int) -> dict | None:
    return one(BOOKING_SQL + " WHERE b.id=?", (booking_id,))


def cancel_booking(booking_id: int, by: str):
    ex("UPDATE bookings SET status='cancelled', cancelled_by=? WHERE id=?", (by, booking_id))


def client_future_bookings(tg_id: int) -> list[dict]:
    return q(BOOKING_SQL + " WHERE c.tg_id=? AND b.status='active' AND b.start>=? ORDER BY b.start",
             (tg_id, now_str()))


# ---------- переписка ----------
def log_message(client_id: int, direction: str, text: str = "", photo: str = "", author: str = "") -> int:
    return ex("""INSERT INTO messages (client_id, direction, text, photo, author, is_read, created_at)
                 VALUES (?,?,?,?,?,?,?)""",
              (client_id, direction, text, photo, author, 1 if direction == "out" else 0,
               datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")))


def add_relay(chat_id: int, message_id: int, client_id: int):
    ex("INSERT OR REPLACE INTO relay (chat_id, message_id, client_id) VALUES (?,?,?)", (chat_id, message_id, client_id))


def relay_client(chat_id: int, message_id: int) -> int | None:
    r = one("SELECT client_id FROM relay WHERE chat_id=? AND message_id=?", (chat_id, message_id))
    return r["client_id"] if r else None


def recently_answered(client_id: int, minutes: int = 30) -> bool:
    """Салон отвечал этому клиенту недавно — значит, идёт живой разговор."""
    r = one("SELECT MAX(created_at) AS t FROM messages WHERE client_id=? AND direction='out'", (client_id,))
    if not r or not r["t"]:
        return False
    last = datetime.strptime(r["t"], "%Y-%m-%d %H:%M:%S")
    return (datetime.now(TZ).replace(tzinfo=None) - last).total_seconds() < minutes * 60


def admin_invite_code() -> str:
    code = settings().get("admin_invite") or ""
    if not code:
        code = secrets.token_hex(5)
        set_setting("admin_invite", code)
    return code


# ---------- вход в кабинет по личной ссылке ----------
LOGIN_DAYS = 30


def issue_login(user_id: int) -> str:
    import time
    token = secrets.token_urlsafe(24)
    ex("INSERT INTO logins (token, user_id, created_at) VALUES (?,?,?)", (token, user_id, time.time()))
    ex("DELETE FROM logins WHERE created_at < ?", (time.time() - LOGIN_DAYS * 86400,))
    return token


def login_user(token: str) -> int | None:
    import time
    r = one("SELECT user_id, created_at FROM logins WHERE token=?", (token,))
    if not r or time.time() - r["created_at"] > LOGIN_DAYS * 86400:
        return None
    return r["user_id"]


# ---------- удаление ----------
def delete_chat(client_id: int):
    """Стереть переписку с клиентом (сам клиент и его записи остаются)."""
    ex("DELETE FROM messages WHERE client_id=?", (client_id,))
    ex("DELETE FROM relay WHERE client_id=?", (client_id,))


def delete_client(client_id: int):
    """Удалить клиента: стираем личные данные и переписку (152-ФЗ — право на удаление).
    Прошлые визиты остаются как «Удалённый клиент», чтобы не ломалась выручка и статистика."""
    delete_chat(client_id)
    # отзывы НЕ удаляем: имя автора хранится в самом отзыве, телефон и переписка стираются
    ex("""UPDATE clients SET name='Удалённый клиент', phone='', note='', username='', tg_id=NULL, deleted=1
          WHERE id=?""", (client_id,))


# ---------- отзывы ----------
REVIEWS_DIR = DB_PATH.parent / "reviews"     # фото к отзывам — рядом с базой салона


def master_ratings() -> dict[int, tuple[float, int]]:
    """{мастер: (средняя оценка, сколько отзывов)} — только видимые отзывы."""
    return {r["master_id"]: (round(r["avg"], 1), r["n"]) for r in
            q("SELECT master_id, AVG(rating) AS avg, COUNT(*) AS n FROM reviews WHERE hidden=0 GROUP BY master_id")}


def stars(avg: float, n: int) -> str:
    return f"★ {avg:.1f} ({n})".replace(".", ",") if n else ""


REVIEW_SQL = """SELECT r.*, c.name AS client_name, m.name AS master_name, b.service_name, b.start
                FROM reviews r JOIN clients c ON c.id=r.client_id JOIN masters m ON m.id=r.master_id
                LEFT JOIN bookings b ON b.id=r.booking_id"""


def get_review(review_id: int) -> dict | None:
    return one(REVIEW_SQL + " WHERE r.id=?", (review_id,))


def first_name(name: str) -> str:
    """В отзывах показываем только имя — без фамилии и контактов."""
    return (name or "Клиент").split()[0]


def review_author(r: dict) -> str:
    return r.get("author") or first_name(r.get("client_name") or "")


def add_review(booking: dict, rating: int) -> int:
    """Создать (или обновить оценку) отзыв о визите. Один отзыв на запись."""
    old = one("SELECT id FROM reviews WHERE booking_id=?", (booking["id"],))
    if old:
        ex("UPDATE reviews SET rating=? WHERE id=?", (rating, old["id"]))
        return old["id"]
    return ex("""INSERT INTO reviews (booking_id, master_id, client_id, rating, author, created_at)
                 VALUES (?,?,?,?,?,?)""",
              (booking["id"], booking["master_id"], booking["client_id"], rating,
               first_name(booking.get("client_name") or ""), now_str()))


def review_token(booking_id: int) -> str:
    """Секретная ссылка на отзыв о визите — для клиентов без бота (отправить в WhatsApp / SMS)."""
    r = one("SELECT review_token FROM bookings WHERE id=?", (booking_id,))
    if r and r["review_token"]:
        return r["review_token"]
    token = secrets.token_urlsafe(12)
    ex("UPDATE bookings SET review_token=? WHERE id=?", (token, booking_id))
    return token


def booking_by_review_token(token: str) -> dict | None:
    return one(BOOKING_SQL + " WHERE b.review_token=? AND b.review_token!=''", (token,)) if token else None


def reviewable_visits(client_tg: int, days: int = 60) -> list[dict]:
    """Прошедшие визиты клиента без отзыва — о них можно написать в любой момент."""
    now = now_local()
    since = (now - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    return q(BOOKING_SQL + """ WHERE c.tg_id=? AND b.status='active' AND b.start>=? AND b.start<?
             AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.booking_id=b.id AND r.text!='') ORDER BY b.start DESC LIMIT 5""",
             (client_tg, since, now.strftime("%Y-%m-%d %H:%M")))


# ---------- формат работы: частный мастер или салон ----------
def is_solo() -> bool:
    return settings().get("mode") == "solo"


def own_master(tg_id: int) -> dict | None:
    """Мастер, которым является сам владелец (у частного мастера — всегда он)."""
    return one("SELECT * FROM masters WHERE tg_id=? AND active=1", (tg_id,)) if tg_id else None
