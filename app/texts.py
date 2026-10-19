"""Форматирование дат и сумм по-русски."""
from datetime import date, datetime

WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def day_human(d: date) -> str:
    return f"{WD_SHORT[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def day_button(d: date, today: date | None = None) -> str:
    if today is not None:
        if d == today:
            return f"Сегодня, {d.day}"
        if (d - today).days == 1:
            return f"Завтра, {d.day}"
    return f"{WD_SHORT[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1][:3]}"


def start_human(start: str) -> str:
    dt = datetime.strptime(start, "%Y-%m-%d %H:%M")
    return f"{day_human(dt.date())} в {dt.strftime('%H:%M')}"


def money(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


def duration_human(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def price(service: dict) -> str:
    return ("от " if service.get("price_from") else "") + money(service["price"])
