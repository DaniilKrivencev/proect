"""Фоновые задачи: напоминания клиентам и вечерний план для мастеров и владельца."""
import asyncio
import logging
from datetime import timedelta

from . import db, notify, slots
from .config import DIGEST_HOUR
from .texts import day_human, money

log = logging.getLogger("reminders")


def due_reminders(now) -> list[tuple[dict, str]]:
    """Какие напоминания пора отправить: (запись, '24' | '2' | 'skip24' | 'skip2')."""
    horizon = (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    rows = db.q("""SELECT id FROM bookings WHERE status='active' AND start>? AND start<=?
                   AND (remind24=0 OR remind2=0)""", (now.strftime("%Y-%m-%d %H:%M"), horizon))
    result = []
    for r in rows:
        b = db.get_booking(r["id"])
        start, created = slots.parse_start(b["start"]), slots.parse_start(b["created_at"])
        left = start - now
        if not b["remind2"] and left <= timedelta(hours=2):
            # записались меньше чем за 3 часа — и так помнят
            result.append((b, "2" if start - created > timedelta(hours=3) else "skip2"))
        elif not b["remind24"] and left <= timedelta(hours=24):
            result.append((b, "24" if start - created > timedelta(hours=25) else "skip24"))
    return result


async def send_reminders():
    now = db.now_local()
    s = db.settings()
    for b, kind in due_reminders(now):
        if kind in ("24", "skip24"):
            db.ex("UPDATE bookings SET remind24=1 WHERE id=?", (b["id"],))
        else:
            db.ex("UPDATE bookings SET remind2=1, remind24=1 WHERE id=?", (b["id"],))
        if kind.startswith("skip"):
            continue
        when = "завтра" if kind == "24" else "через 2 часа"
        addr = f"\n📍 {s['address']}" if s["address"] else ""
        await notify.send(
            b["client_tg"],
            f"⏰ Напоминаю: {when} у вас запись в «{s['salon_name']}»\n\n{notify.booking_line(b)}\n"
            f"👩 Мастер: {b['master_name']}{addr}\n\n"
            f"Если планы изменились — отмените запись заранее, чтобы окошко занял кто-то другой.",
            notify.InlineKeyboardMarkup(inline_keyboard=[[notify.InlineKeyboardButton(
                text="Отменить запись", callback_data=notify.CCancel(b=b["id"]).pack())]]))


def _lines(rows, with_master: bool) -> str:
    return "\n".join(f"{r['start'][11:]} — {r['client_name']}, {r['service_name']}"
                     + (f" · {r['master_name']}" if with_master else "") for r in rows)


async def send_digests():
    now = db.now_local()
    if now.hour < DIGEST_HOUR:
        return
    today = now.date().isoformat()
    tomorrow = now.date() + timedelta(days=1)
    head = f"🌙 План на завтра, {day_human(tomorrow)}"
    for m in db.masters():
        if m["last_digest"] == today:
            continue
        db.ex("UPDATE masters SET last_digest=? WHERE id=?", (today, m["id"]))
        rows = db.q(db.BOOKING_SQL + " WHERE b.master_id=? AND b.status='active' AND b.start LIKE ? ORDER BY b.start",
                    (m["id"], tomorrow.isoformat() + "%"))
        if rows and m["tg_id"] and not db.is_admin(m["tg_id"]):
            await notify.send(m["tg_id"], f"{head}:\n\n{_lines(rows, False)}\n\nВсего записей: {len(rows)}")
    if db.settings().get("admin_digest") != today:
        db.set_setting("admin_digest", today)
        rows = db.q(db.BOOKING_SQL + " WHERE b.status='active' AND b.start LIKE ? ORDER BY b.start",
                    (tomorrow.isoformat() + "%",))
        if rows:
            text = (f"{head}:\n\n{_lines(rows, True)}\n\n"
                    f"Всего записей: {len(rows)} · ожидаемо {money(sum(r['price'] for r in rows))}")
            for a in db.admins():
                await notify.send(a["tg_id"], text)


async def loop():
    while True:
        try:
            await send_reminders()
            await send_digests()
            from . import demo, reviews
            await reviews.send_requests()
            demo.tick()
        except Exception:
            log.exception("Ошибка в фоновых задачах")
        await asyncio.sleep(60)
