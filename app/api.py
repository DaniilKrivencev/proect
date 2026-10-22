"""Сервер кабинета салона (Telegram Mini App). Каждый запрос проверяется подписью Telegram.

Роли: владелец (admin) видит и меняет всё; мастер видит только свои записи и свой график.
"""
import logging
import re
import secrets
from datetime import date, timedelta

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, notify, slots
from .config import BASE_DIR, BOT_TOKEN, DEV_USER_ID, SLEEPING_CLIENT_DAYS, runtime

log = logging.getLogger("api")
app = FastAPI(title="Лунка", docs_url=None, redoc_url=None)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------- кто открыл кабинет ----------
def current_user(response: Response, x_init_data: str = Header(default=""), x_login: str = Header(default="")) -> dict:
    if x_login:  # вход по личной ссылке (сотрудники из MAX, браузер)
        tg_id = db.login_user(x_login)
        if tg_id is None:
            raise HTTPException(401, "Ссылка входа устарела — получите новую в боте: /start → «Открыть кабинет»")
        name = ""
    elif DEV_USER_ID and x_init_data == "dev":
        tg_id, name = DEV_USER_ID, "Тестовый владелец"
        if not db.admins():
            db.add_admin(tg_id, name)
    else:
        try:
            data = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=x_init_data)
        except ValueError:
            raise HTTPException(401, "Откройте кабинет из Telegram")
        if not data.user:
            raise HTTPException(401, "Нет данных пользователя")
        tg_id = data.user.id
        name = " ".join(filter(None, [data.user.first_name, data.user.last_name]))
    support = bool(db.dev_mode(tg_id))  # техподдержка: телефоны клиентов ей не показываем
    if support:
        response.headers["x-support"] = "1"
    if db.is_admin(tg_id):
        if not name:
            name = (db.one("SELECT name FROM admins WHERE tg_id=?", (tg_id,)) or {}).get("name", "") or "Техподдержка"
        return {"role": "admin", "tg_id": tg_id, "master_id": None, "name": name, "support": support}
    m = db.master_by_tg(tg_id)
    if m:
        return {"role": "master", "tg_id": tg_id, "master_id": m["id"], "name": m["name"], "support": support}
    raise HTTPException(403, "Кабинет доступен только сотрудникам салона")


# ---------- техподдержка не видит телефоны клиентов ----------
import json as _json  # noqa: E402

from starlette.requests import Request  # noqa: E402

PHONE_KEYS = {"phone", "client_phone"}


def mask_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return f"+7 *** ***-**-{digits[-2:]}" if len(digits) >= 4 else ("скрыт" if p else "")


def _mask(obj, parent: str = ""):
    if isinstance(obj, dict):
        return {k: (mask_phone(v) if k in PHONE_KEYS and isinstance(v, str) and parent != "salon"
                    else _mask(v, k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x, parent) for x in obj]
    return obj


@app.middleware("http")
async def hide_phones_from_support(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("x-support") != "1":
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        data = _mask(_json.loads(body))
        body = _json.dumps(data, ensure_ascii=False).encode()
    except Exception:
        pass
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "x-support")}
    return Response(body, status_code=response.status_code, headers=headers, media_type="application/json")


def admin_only(u=Depends(current_user)) -> dict:
    if u["role"] != "admin":
        raise HTTPException(403, "Это может сделать только владелец салона")
    return u


def scope_master(u: dict, master_id: int | None) -> int | None:
    """Мастер всегда видит только себя; владелец — кого выбрал (или всех)."""
    return u["master_id"] if u["role"] == "master" else (master_id or None)


def can_edit_master(u: dict, master_id: int):
    if u["role"] == "master" and u["master_id"] != master_id:
        raise HTTPException(403, "Можно менять только свой график")


def invite_link(m: dict) -> str:
    return f"https://t.me/{runtime['bot_username']}?start=staff_{m['invite']}" if runtime["bot_username"] else ""


def invite_link_max(m: dict) -> str:
    return f"https://max.ru/{runtime['max_username']}?start=staff_{m['invite']}" if runtime.get("max_username") else ""


# ---------- профиль, статистика ----------
@app.get("/api/me")
def me(u=Depends(current_user)):
    mid = scope_master(u, None)
    where, params = ("AND b.master_id=?", (mid,)) if mid else ("", ())
    now = db.now_local()
    stats = db.one(f"""SELECT COUNT(*) AS visits, COALESCE(SUM(price),0) AS revenue FROM bookings b
                       WHERE status='active' AND start>=? AND start<=? {where}""",
                   (now.strftime("%Y-%m-01 00:00"), db.now_str(), *params))
    upcoming = db.one(f"SELECT COUNT(*) AS n FROM bookings b WHERE status='active' AND start>? {where}",
                      (db.now_str(), *params))
    clients = db.one("SELECT COUNT(*) AS n FROM clients")
    nxt = db.one(db.BOOKING_SQL + f" WHERE b.status='active' AND b.start>=? {where} ORDER BY b.start LIMIT 1",
                 (db.now_str(), *params))
    s = db.settings()
    link = f"https://t.me/{runtime['bot_username']}" if runtime["bot_username"] else ""
    unread = db.one("SELECT COUNT(DISTINCT client_id) AS n FROM messages WHERE direction='in' AND is_read=0")["n"]
    from .niche import KEY as niche
    return {"role": u["role"], "name": u["name"], "master_id": u["master_id"], "niche": niche,
            "salon": {k: s.get(k, "") for k in ("salon_name", "address", "phone", "about", "slot_step")},
            "link": link, "next": nxt,
            "link_max": f"https://max.ru/{runtime['max_username']}" if runtime.get("max_username") else "", "today": now.date().isoformat(),
            "unread_chats": unread if u["role"] == "admin" else 0,
            "support_access": bool(db.dev_id()) if u["role"] == "admin" else False,
            "stats": {"month_revenue": stats["revenue"], "month_visits": stats["visits"],
                      "upcoming": upcoming["n"], "clients": clients["n"], "masters": len(db.masters())}}


class SalonIn(BaseModel):
    salon_name: str = Field(min_length=1, max_length=80)
    address: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=30)
    about: str = Field(default="", max_length=500)
    slot_step: int = Field(default=30, ge=10, le=120)


@app.put("/api/salon")
def update_salon(p: SalonIn, u=Depends(admin_only)):
    for k, v in p.model_dump().items():
        db.set_setting(k, v.strip() if isinstance(v, str) else v)
    return {"ok": True}


# ---------- услуги ----------
class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    price: int = Field(ge=0, le=1_000_000)
    duration: int = Field(ge=10, le=600)
    price_from: bool = False


@app.get("/api/services")
def list_services(u=Depends(current_user)):
    return db.services()


@app.post("/api/services")
def add_service(s: ServiceIn, u=Depends(admin_only)):
    sid = db.ex("INSERT INTO services (name, price, duration, price_from) VALUES (?,?,?,?)",
                (s.name.strip(), s.price, s.duration, int(s.price_from)))
    for m in db.masters():  # новую услугу по умолчанию делают все мастера — снять галочку можно в карточке
        db.ex("INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?,?)", (m["id"], sid))
    return db.get_service(sid)


def _service(service_id: int) -> dict:
    s = db.get_service(service_id)
    if not s or not s["active"]:
        raise HTTPException(404, "Услуга не найдена")
    return s


@app.put("/api/services/{service_id}")
def edit_service(service_id: int, s: ServiceIn, u=Depends(admin_only)):
    _service(service_id)
    db.ex("UPDATE services SET name=?, price=?, duration=?, price_from=? WHERE id=?",
          (s.name.strip(), s.price, s.duration, int(s.price_from), service_id))
    return db.get_service(service_id)


@app.delete("/api/services/{service_id}")
def delete_service(service_id: int, u=Depends(admin_only)):
    _service(service_id)
    db.ex("UPDATE services SET active=0 WHERE id=?", (service_id,))
    return {"ok": True}


# ---------- мастера ----------
def master_view(m: dict) -> dict:
    sch = db.schedule(m["id"])
    up = db.one("SELECT COUNT(*) AS n FROM bookings WHERE master_id=? AND status='active' AND start>?",
                (m["id"], db.now_str()))
    today = db.now_local().date().isoformat()
    return {**{k: m[k] for k in ("id", "name", "spec")}, "connected": bool(m["tg_id"]),
            "invite_link": invite_link(m), "invite_link_max": invite_link_max(m), "upcoming": up["n"],
            "service_ids": db.master_service_ids(m["id"]),
            "schedule": [{"weekday": wd, "works": wd in sch, "start": sch.get(wd, ("10:00", "20:00"))[0],
                          "end": sch.get(wd, ("10:00", "20:00"))[1]} for wd in range(7)],
            "days_off": [r["day"] for r in db.q("SELECT day FROM days_off WHERE master_id=? AND day>=? ORDER BY day",
                                                (m["id"], today))]}


def _master(master_id: int) -> dict:
    m = db.get_master(master_id)
    if not m or not m["active"]:
        raise HTTPException(404, "Мастер не найден")
    return m


@app.get("/api/masters")
def list_masters(u=Depends(current_user)):
    return [master_view(m) for m in db.masters()]


class MasterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    spec: str = Field(default="", max_length=100)


@app.post("/api/masters")
def add_master(p: MasterIn, u=Depends(admin_only)):
    return master_view(db.create_master(p.name.strip(), p.spec.strip()))


class DayIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    works: bool
    start: str = "10:00"
    end: str = "20:00"


class MasterEditIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    spec: str | None = Field(default=None, max_length=100)
    service_ids: list[int] | None = None
    schedule: list[DayIn] | None = None


@app.put("/api/masters/{master_id}")
def edit_master(master_id: int, p: MasterEditIn, u=Depends(current_user)):
    _master(master_id)
    can_edit_master(u, master_id)
    if u["role"] == "admin":
        if p.name is not None:
            db.ex("UPDATE masters SET name=? WHERE id=?", (p.name.strip(), master_id))
        if p.spec is not None:
            db.ex("UPDATE masters SET spec=? WHERE id=?", (p.spec.strip(), master_id))
        if p.service_ids is not None:
            db.ex("DELETE FROM master_services WHERE master_id=?", (master_id,))
            for sid in p.service_ids:
                if db.get_service(sid):
                    db.ex("INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?,?)", (master_id, sid))
    if p.schedule is not None:
        for d in p.schedule:
            if d.works and (not (TIME_RE.match(d.start) and TIME_RE.match(d.end)) or d.start >= d.end):
                raise HTTPException(400, "Проверьте время работы: начало должно быть раньше конца")
        for d in p.schedule:
            if d.works:
                db.ex('INSERT OR REPLACE INTO schedule (master_id, weekday, start, "end") VALUES (?,?,?,?)',
                      (master_id, d.weekday, d.start, d.end))
            else:
                db.ex("DELETE FROM schedule WHERE master_id=? AND weekday=?", (master_id, d.weekday))
    return master_view(db.get_master(master_id))


@app.delete("/api/masters/{master_id}")
def remove_master(master_id: int, u=Depends(admin_only)):
    _master(master_id)
    future = db.one("SELECT COUNT(*) AS n FROM bookings WHERE master_id=? AND status='active' AND start>?",
                    (master_id, db.now_str()))["n"]
    if future:
        raise HTTPException(409, f"У мастера ещё {future} предстоящих записей. Отмените или перенесите их, "
                                 f"потом удалите мастера.")
    db.ex("UPDATE masters SET active=0, tg_id=NULL WHERE id=?", (master_id,))
    return {"ok": True}


@app.post("/api/masters/{master_id}/new_invite")
def new_invite(master_id: int, u=Depends(admin_only)):
    """Новая ссылка-приглашение; старый Telegram мастера отключается."""
    _master(master_id)
    db.ex("UPDATE masters SET invite=?, tg_id=NULL WHERE id=?", (secrets.token_hex(5), master_id))
    return master_view(db.get_master(master_id))


@app.post("/api/days_off/{master_id}/{day}")
def toggle_day_off(master_id: int, day: date, u=Depends(current_user)):
    _master(master_id)
    can_edit_master(u, master_id)
    d = day.isoformat()
    if db.is_day_off(master_id, d):
        db.ex("DELETE FROM days_off WHERE master_id=? AND day=?", (master_id, d))
        return {"day_off": False}
    db.ex("INSERT INTO days_off (master_id, day) VALUES (?,?)", (master_id, d))
    return {"day_off": True}


# ---------- записи ----------
@app.get("/api/bookings")
def list_bookings(day: date, master_id: int = 0, u=Depends(current_user)):
    mid = scope_master(u, master_id)
    where, params = ("AND b.master_id=?", (mid,)) if mid else ("", ())
    rows = db.q(db.BOOKING_SQL + f" WHERE b.start LIKE ? {where} ORDER BY b.start, b.master_id",
                (day.isoformat() + "%", *params))
    info = None
    if mid:
        sch = db.schedule(mid).get(day.weekday())
        info = {"day_off": db.is_day_off(mid, day.isoformat()), "hours": list(sch) if sch else None}
    else:
        working = [m["name"] for m in db.masters()
                   if db.schedule(m["id"]).get(day.weekday()) and not db.is_day_off(m["id"], day.isoformat())]
        info = {"working": working}
    return {"bookings": rows, **info}


@app.get("/api/days_summary")
def days_summary(master_id: int = 0, u=Depends(current_user)):
    mid = scope_master(u, master_id)
    where, params = ("AND master_id=?", (mid,)) if mid else ("", ())
    today = db.now_local().date()
    rows = db.q(f"""SELECT substr(start,1,10) AS day, COUNT(*) AS n FROM bookings
                    WHERE status='active' AND start>=? AND start<? {where} GROUP BY day""",
                (today.isoformat(), (today + timedelta(days=14)).isoformat(), *params))
    return {r["day"]: r["n"] for r in rows}


@app.get("/api/slots")
def get_slots(service_id: int, day: date, master_id: int, u=Depends(current_user)):
    s = _service(service_id)
    mid = scope_master(u, master_id)
    return slots.free_slots(mid, day, s["duration"]) if mid else []


class ManualBookingIn(BaseModel):
    client_name: str = Field(min_length=1, max_length=80)
    client_phone: str = Field(default="", max_length=30)
    client_id: int | None = None
    service_id: int
    master_id: int
    day: date
    time: str


@app.post("/api/bookings")
def add_booking(b: ManualBookingIn, u=Depends(current_user)):
    s = _service(b.service_id)
    mid = scope_master(u, b.master_id)
    _master(mid)
    if not TIME_RE.match(b.time):
        raise HTTPException(400, "Неверное время")
    if not slots.fits(b.time, s["duration"], db.busy_intervals(mid, b.day.isoformat())):
        raise HTTPException(409, "У мастера на это время уже есть запись")
    if b.client_id:
        client = db.one("SELECT * FROM clients WHERE id=?", (b.client_id,))
        if not client:
            raise HTTPException(404, "Клиент не найден")
    else:
        client = db.get_or_create_client(None, b.client_name.strip(), b.client_phone.strip())
    bid = db.create_booking(mid, client["id"], s, f"{b.day.isoformat()} {b.time}")
    return db.get_booking(bid)


@app.post("/api/bookings/{booking_id}/cancel")
async def cancel(booking_id: int, u=Depends(current_user)):
    b = db.get_booking(booking_id)
    if not b or (u["role"] == "master" and b["master_id"] != u["master_id"]):
        raise HTTPException(404, "Запись не найдена")
    if b["status"] == "active":
        db.cancel_booking(booking_id, "salon")
        await notify.client_cancelled_by_salon(b)
    return {"ok": True}


# ---------- клиенты ----------
@app.get("/api/clients")
def list_clients(u=Depends(current_user)):
    now = db.now_str()
    mid = u["master_id"] if u["role"] == "master" else None
    bf = "AND b.master_id=?" if mid else ""
    p = (mid,) if mid else ()
    rows = db.q(f"""
        SELECT c.*,
          (SELECT COUNT(*) FROM bookings b WHERE b.client_id=c.id AND b.status='active' AND b.start<=? {bf}) AS visits,
          (SELECT COALESCE(SUM(price),0) FROM bookings b WHERE b.client_id=c.id AND b.status='active' AND b.start<=? {bf}) AS spent,
          (SELECT MAX(start) FROM bookings b WHERE b.client_id=c.id AND b.status='active' AND b.start<=? {bf}) AS last_visit,
          (SELECT MIN(start) FROM bookings b WHERE b.client_id=c.id AND b.status='active' AND b.start>? {bf}) AS next_visit,
          (SELECT m.name FROM bookings b JOIN masters m ON m.id=b.master_id WHERE b.client_id=c.id
             AND b.status='active' GROUP BY m.id ORDER BY COUNT(*) DESC LIMIT 1) AS fav_master
        FROM clients c WHERE c.deleted=0 ORDER BY last_visit DESC""", (now, *p, now, *p, now, *p, now, *p))
    if mid:
        rows = [r for r in rows if r["visits"] or r["next_visit"]]
    border = (db.now_local() - timedelta(days=SLEEPING_CLIENT_DAYS)).strftime("%Y-%m-%d %H:%M")
    for r in rows:
        r["sleeping"] = bool(r["last_visit"] and r["last_visit"] < border and not r["next_visit"])
    return rows


class ClientIn(BaseModel):
    note: str = Field(default="", max_length=1000)
    name: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=30)


@app.put("/api/clients/{client_id}")
def edit_client(client_id: int, n: ClientIn, u=Depends(current_user)):
    c = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    if not c:
        raise HTTPException(404, "Клиент не найден")
    phone = c["phone"] if (u.get("support") or n.phone is None) else n.phone  # техподдержка видит маску — не затираем
    db.ex("UPDATE clients SET note=?, name=?, phone=? WHERE id=?",
          (n.note, (n.name or c["name"]).strip(), phone.strip(), client_id))
    return {"ok": True}


@app.post("/api/clients/{client_id}/invite")
async def invite_client(client_id: int, u=Depends(current_user)):
    c = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    if not c or not c["tg_id"]:
        raise HTTPException(400, "Клиент не подключён к боту — позвоните или напишите ему")
    if not await notify.invite_back(c):
        raise HTTPException(400, "Не удалось отправить: клиент мог заблокировать бота")
    return {"ok": True}


# ---------- переписка с клиентками (владелец и администраторы) ----------
@app.get("/api/chats")
def list_chats(u=Depends(admin_only)):
    return db.q("""
        SELECT c.id, c.name, c.phone, c.username,
               m.text AS last_text, m.photo AS last_photo, m.direction AS last_dir, m.created_at AS last_at,
               (SELECT COUNT(*) FROM messages x WHERE x.client_id=c.id AND x.direction='in' AND x.is_read=0) AS unread
        FROM clients c
        JOIN messages m ON m.id = (SELECT MAX(id) FROM messages WHERE client_id=c.id)
        ORDER BY m.id DESC""")


@app.get("/api/chats/{client_id}")
def chat_thread(client_id: int, u=Depends(admin_only)):
    c = db.one("SELECT id, name, phone, username, tg_id FROM clients WHERE id=?", (client_id,))
    if not c:
        raise HTTPException(404, "Клиентка не найдена")
    db.ex("UPDATE messages SET is_read=1 WHERE client_id=? AND direction='in'", (client_id,))
    msgs = db.q("SELECT id, direction, text, photo != '' AS has_photo, author, created_at FROM messages "
                "WHERE client_id=? ORDER BY id DESC LIMIT 200", (client_id,))[::-1]
    nxt = db.one(db.BOOKING_SQL + " WHERE b.client_id=? AND b.status='active' AND b.start>=? ORDER BY b.start LIMIT 1",
                 (client_id, db.now_str()))
    return {"client": {**c, "can_reply": bool(c["tg_id"])}, "messages": msgs,
            "next": {"start": nxt["start"], "service": nxt["service_name"], "master": nxt["master_name"]} if nxt else None}


class ChatMsgIn(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


@app.post("/api/chats/{client_id}")
async def chat_send(client_id: int, p: ChatMsgIn, u=Depends(admin_only)):
    c = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    if not c or not c["tg_id"]:
        raise HTTPException(400, "Клиентка не подключена к боту — позвоните ей")
    admin = db.one("SELECT name FROM admins WHERE tg_id=?", (u["tg_id"],))
    if not await notify.salon_reply(c, p.text.strip(), admin["name"] if admin else "Салон"):
        raise HTTPException(400, "Не удалось отправить: клиентка могла заблокировать бота")
    return {"ok": True}


@app.get("/api/admin_invite")
def admin_invite(u=Depends(admin_only)):
    """Одноразовая ссылка, чтобы подключить директора/администратора."""
    code = db.admin_invite_code()
    return {"link": f"https://t.me/{runtime['bot_username']}?start=admin_{code}" if runtime["bot_username"] else "",
            "link_max": f"https://max.ru/{runtime['max_username']}?start=admin_{code}" if runtime.get("max_username") else "",
            "admins": [a["name"] for a in db.admins()]}


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int, u=Depends(admin_only)):
    c = db.one("SELECT * FROM clients WHERE id=? AND deleted=0", (client_id,))
    if not c:
        raise HTTPException(404, "Клиент не найден")
    future = db.one("SELECT COUNT(*) AS n FROM bookings WHERE client_id=? AND status='active' AND start>?",
                    (client_id, db.now_str()))["n"]
    if future:
        raise HTTPException(409, f"У клиента {future} предстоящих записей. Сначала отмените их во вкладке «Записи».")
    db.delete_client(client_id)
    return {"ok": True}


@app.delete("/api/chats/{client_id}")
def delete_chat(client_id: int, u=Depends(admin_only)):
    if not db.one("SELECT 1 FROM clients WHERE id=?", (client_id,)):
        raise HTTPException(404, "Переписка не найдена")
    db.delete_chat(client_id)
    return {"ok": True}


# ---------- отзывы ----------
@app.get("/api/reviews")
def list_reviews(master_id: int = 0, u=Depends(current_user)):
    mid = scope_master(u, master_id)
    where, params = ("WHERE r.master_id=?", (mid,)) if mid else ("", ())
    if u["role"] == "master":
        where += " AND r.hidden=0"
    rows = db.q(db.REVIEW_SQL + f" {where} ORDER BY r.id DESC LIMIT 200", params)
    rt = db.master_ratings()
    return {
        "reviews": [{"id": r["id"], "rating": r["rating"], "text": r["text"], "client": db.review_author(r),
                     "master_id": r["master_id"], "master": r["master_name"], "service": r["service_name"] or "",
                     "date": (r["start"] or r["created_at"])[:10], "photo": f"r/{r['photo']}" if r["photo"] else "",
                     "reply": r["reply"], "reply_by": r["reply_by"], "hidden": bool(r["hidden"])} for r in rows],
        "ratings": {str(k): {"avg": v[0], "n": v[1]} for k, v in rt.items()},
    }


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


def _review_for(u: dict, review_id: int) -> dict:
    r = db.get_review(review_id)
    if not r or (u["role"] == "master" and r["master_id"] != u["master_id"]):
        raise HTTPException(404, "Отзыв не найден")
    return r


@app.post("/api/reviews/{review_id}/reply")
async def reply_review(review_id: int, p: ReplyIn, u=Depends(current_user)):
    r = _review_for(u, review_id)
    from . import reviews
    who = u["name"] if u["role"] == "master" else "Салон"
    await reviews.save_reply(r, p.text.strip(), who)
    return {"ok": True}


@app.post("/api/reviews/{review_id}/hide")
def hide_review(review_id: int, u=Depends(admin_only)):
    r = _review_for(u, review_id)
    db.ex("UPDATE reviews SET hidden=? WHERE id=?", (0 if r["hidden"] else 1, review_id))
    return {"hidden": not r["hidden"]}


@app.get("/api/bookings/{booking_id}/review_link")
def review_link(booking_id: int, u=Depends(current_user)):
    """Ссылка «оцените визит» для клиента без бота — владелец/мастер отправляет её в WhatsApp или SMS."""
    b = db.get_booking(booking_id)
    if not b or (u["role"] == "master" and b["master_id"] != u["master_id"]):
        raise HTTPException(404, "Запись не найдена")
    if not runtime.get("webapp_url"):
        raise HTTPException(409, "Кабинет сейчас без постоянного адреса — ссылку сделать нельзя")
    link = f"{runtime['webapp_url']}/review.html?t={db.review_token(booking_id)}"
    salon = db.settings()["salon_name"]
    text = (f"Здравствуйте, {db.first_name(b['client_name'])}! Спасибо, что были в «{salon}» 🌸 "
            f"Оцените, пожалуйста, визит к мастеру {b['master_name']} — это займёт минуту:")
    return {"link": link, "text": text}


class PublicReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=1000)
    photo: str = Field(default="", max_length=12_000_000)  # data:image/jpeg;base64,...


def _public_booking(token: str) -> dict:
    b = db.booking_by_review_token(token)
    if not b or b["status"] != "active":
        raise HTTPException(404, "Ссылка не найдена — попросите у салона новую")
    return b


@app.get("/api/public/review/{token}")
def public_review_get(token: str):
    b = _public_booking(token)
    r = db.one("SELECT rating, text FROM reviews WHERE booking_id=?", (b["id"],))
    return {"salon": db.settings()["salon_name"], "master": b["master_name"], "service": b["service_name"],
            "start": b["start"], "started": b["start"] <= db.now_str(), "review": r}


@app.post("/api/public/review/{token}")
async def public_review_post(token: str, p: PublicReviewIn):
    import base64
    b = _public_booking(token)
    if b["start"] > db.now_str():
        raise HTTPException(409, "Оставить отзыв можно после визита")
    raw = b""
    if p.photo:  # сначала проверяем фото — чтобы при ошибке ничего не записать наполовину
        try:
            raw = base64.b64decode(p.photo.split(",", 1)[-1], validate=True)
        except Exception:
            raise HTTPException(400, "Фото не получилось прочитать")
        if not raw.startswith(b"\xff\xd8"):
            raise HTTPException(400, "Нужна фотография (JPEG)")
    first = not db.one("SELECT 1 FROM reviews WHERE booking_id=? AND (text!='' OR photo!='')", (b["id"],))
    rid = db.add_review(b, p.rating)
    db.ex("UPDATE reviews SET text=? WHERE id=?", (p.text.strip(), rid))
    if raw:
        db.REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        name = secrets.token_hex(12) + ".jpg"
        (db.REVIEWS_DIR / name).write_bytes(raw)
        db.ex("UPDATE reviews SET photo=? WHERE id=?", (name, rid))
    db.ex("UPDATE bookings SET review_asked=1 WHERE id=?", (b["id"],))
    if first:
        from . import reviews
        await reviews.notify_staff(db.get_review(rid))
    return {"ok": True}


PHOTO_RE = re.compile(r"^[0-9a-f]{24}\.jpg$")


@app.get("/r/{name}")
def review_photo(name: str):
    """Фото к отзыву — открытая ссылка со случайным именем (её видят клиенты в боте)."""
    from fastapi.responses import FileResponse
    path = db.REVIEWS_DIR / name
    if not PHOTO_RE.match(name) or not path.is_file():
        raise HTTPException(404, "Фото не найдено")
    r = db.one("SELECT hidden FROM reviews WHERE photo=?", (name,))
    if not r or r["hidden"]:
        raise HTTPException(404, "Фото не найдено")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


app.mount("/", StaticFiles(directory=BASE_DIR / "webapp", html=True), name="webapp")
