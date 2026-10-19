"""Расчёт свободных окошек: у конкретного мастера или у любого свободного."""
from datetime import date, datetime, timedelta

from . import db
from .config import BOOK_DAYS_AHEAD, MIN_LEAD_MINUTES


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def compute_slots(work_start: str, work_end: str, step: int, duration: int,
                  busy: list[tuple[str, int]], earliest_min: int | None = None) -> list[str]:
    """Время начала, когда услуга целиком помещается в рабочее время и не пересекается с занятым."""
    ws, we = to_min(work_start), to_min(work_end)
    busy_ranges = [(to_min(s), to_min(s) + d) for s, d in busy]
    result = []
    t = ws
    while t + duration <= we:
        if earliest_min is None or t >= earliest_min:
            if all(t + duration <= bs or t >= be for bs, be in busy_ranges):
                result.append(to_hhmm(t))
        t += max(step, 5)
    return result


def fits(start: str, duration: int, busy: list[tuple[str, int]]) -> bool:
    s = to_min(start)
    if s + duration > 24 * 60:
        return False
    return all(s + duration <= to_min(b) or s >= to_min(b) + d for b, d in busy)


def free_slots(master_id: int, day: date, duration: int) -> list[str]:
    m = db.get_master(master_id)
    if not m or not m["active"]:
        return []
    day_s = day.isoformat()
    if db.is_day_off(master_id, day_s):
        return []
    hours = db.schedule(master_id).get(day.weekday())
    if not hours:
        return []
    now = db.now_local()
    if day < now.date():
        return []
    earliest = now.hour * 60 + now.minute + MIN_LEAD_MINUTES if day == now.date() else None
    return compute_slots(hours[0], hours[1], db.slot_step(), duration, db.busy_intervals(master_id, day_s), earliest)


def slots_by_time(service: dict, day: date, master_id: int = 0) -> dict[str, list[int]]:
    """{время: [id мастеров, у которых оно свободно]}; master_id=0 — любой мастер."""
    ids = [master_id] if master_id else [m["id"] for m in db.masters_for_service(service["id"])]
    result: dict[str, list[int]] = {}
    for mid in ids:
        for t in free_slots(mid, day, service["duration"]):
            result.setdefault(t, []).append(mid)
    return dict(sorted(result.items()))


def available_days(service: dict, master_id: int = 0) -> list[date]:
    today = db.now_local().date()
    days = [today + timedelta(days=i) for i in range(BOOK_DAYS_AHEAD)]
    return [d for d in days if slots_by_time(service, d, master_id)]


def pick_master(service: dict, day: date, hhmm: str, master_id: int = 0) -> int | None:
    """Кого записать на это время. Для «любого мастера» — того, у кого в этот день меньше записей."""
    candidates = slots_by_time(service, day, master_id).get(hhmm, [])
    if not candidates:
        return None
    return min(candidates, key=lambda mid: len(db.busy_intervals(mid, day.isoformat())))


def parse_start(start: str) -> datetime:
    return datetime.strptime(start, "%Y-%m-%d %H:%M")
