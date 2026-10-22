"""Отзывы о мастерах: после визита бот просит оценку (1–5 звёзд), текст и фото.
Мастер или владелец отвечает прямо в мессенджере; клиенты видят рейтинг при выборе мастера
и могут почитать отзывы (кнопка «⭐ Отзывы» в меню). Работает одинаково в Telegram и MAX."""
import html
import logging
import secrets
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from . import db, notify
from .bot import B_REVIEWS, MENU, MENU_TEXTS, btn, full_name, kb, safe_edit
from .config import runtime
from .texts import start_human

log = logging.getLogger("reviews")
router = Router()

ASK_AFTER = timedelta(hours=2)    # через сколько после окончания визита просим отзыв
ASK_WITHIN = timedelta(days=3)    # старше — уже не спрашиваем
ASK_HOURS = (10, 21)              # не беспокоим ночью
PAGE = 5
RATING_WORDS = {5: "Отлично", 4: "Хорошо", 3: "Нормально", 2: "Плохо", 1: "Ужасно"}


class Rate(CallbackData, prefix="rv"):
    b: int   # запись
    r: int   # оценка, 0 — «не сейчас»


class ReplyTo(CallbackData, prefix="rr"):
    v: int   # отзыв


class ShowRev(CallbackData, prefix="sr"):
    m: int   # мастер (0 — список мастеров)
    o: int = 0  # сдвиг для «ещё»


class ReviewStates(StatesGroup):
    text = State()
    photo = State()


class ReplyStates(StatesGroup):
    text = State()


def stars_line(r: int) -> str:
    return "★" * r + "☆" * (5 - r)


def photo_url(name: str) -> str:
    base = runtime.get("webapp_url") or ""
    return f"{base}/r/{name}" if base and name else ""


# ======================================================================
#  1. Просьба оставить отзыв (вызывается из фоновых задач раз в минуту)
# ======================================================================
def due_requests(now: datetime) -> list[dict]:
    if not (ASK_HOURS[0] <= now.hour < ASK_HOURS[1]):
        return []
    since = (now - ASK_WITHIN).strftime("%Y-%m-%d %H:%M")
    rows = db.q(db.BOOKING_SQL + " WHERE b.status='active' AND b.review_asked=0 AND b.start>=? AND b.start<?",
                (since, now.strftime("%Y-%m-%d %H:%M")))
    out = []
    for b in rows:
        end = datetime.strptime(b["start"], "%Y-%m-%d %H:%M") + timedelta(minutes=b["duration"])
        if now >= end + ASK_AFTER:
            out.append(b)
    return out


async def send_requests():
    for b in due_requests(db.now_local()):
        db.ex("UPDATE bookings SET review_asked=1 WHERE id=?", (b["id"],))
        if not b["client_tg"]:
            continue  # записан вручную, без бота — написать некуда
        rows = [[btn(f"{'⭐' * r} {RATING_WORDS[r]}", Rate(b=b["id"], r=r))] for r in (5, 4, 3, 2, 1)]
        rows.append([btn("Не сейчас", Rate(b=b["id"], r=0))])  # позже — «⭐ Отзывы» → «Оставить отзыв»
        await notify.send(b["client_tg"],
                          f"Спасибо, что были у нас! 🌸\n\nКак вам визит к мастеру <b>{html.escape(b['master_name'])}</b> "
                          f"({html.escape(b['service_name'])})?\nОцените, пожалуйста, — это займёт 10 секунд и очень "
                          f"поможет мастеру 🙏", kb(rows))


# ======================================================================
#  2. Клиент оставляет отзыв: звёзды → текст → фото
# ======================================================================
@router.callback_query(Rate.filter())
async def rate(cb: CallbackQuery, callback_data: Rate, state: FSMContext):
    b = db.get_booking(callback_data.b)
    if not b or b["client_tg"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if not callback_data.r:
        await cb.answer()
        await safe_edit(cb.message, "Хорошо 🙂 Оставить отзыв можно в любой момент: кнопка «⭐ Отзывы» → «Оставить отзыв».")
        return
    r = callback_data.r
    rid = db.add_review(b, r)
    await cb.answer("Спасибо!")
    await state.set_state(ReviewStates.text)
    await state.update_data(review_id=rid)
    await safe_edit(cb.message,
                    f"Ваша оценка: {stars_line(r)}\n\n"
                    "Напишите пару слов о визите 💬 — что понравилось, что можно сделать лучше.\n"
                    "Можно сразу прислать фото результата 📸",
                    kb([[btn("Пропустить", "rv_skip")]]))


async def _review_id(state: FSMContext) -> int | None:
    return (await state.get_data()).get("review_id")


async def _save_photo(message: Message) -> str:
    """Скачать фото к себе: tenants/<салон>/reviews/<случайное имя>.jpg (Telegram и MAX)."""
    db.REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(12) + ".jpg"
    path = db.REVIEWS_DIR / name
    if message.chat.id < 0:  # MAX: у фото есть прямая ссылка
        import httpx
        from .maxbot import ssl_context
        ms = getattr(message.bot.session, "max_session", None)
        info = (ms.ids.get(message.message_id) if ms else None) or {}
        url = (info.get("photo") or {}).get("url")
        if not url:
            raise RuntimeError("нет ссылки на фото")
        async with httpx.AsyncClient(timeout=60, verify=ssl_context(), follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            path.write_bytes(r.content)
    else:
        await message.bot.download(message.photo[-1].file_id, destination=path)
    return name


@router.message(ReviewStates.text, F.photo)
@router.message(ReviewStates.photo, F.photo)
async def got_photo(message: Message, state: FSMContext):
    rid = await _review_id(state)
    if not rid:
        await state.clear()
        return
    try:
        name = await _save_photo(message)
        db.ex("UPDATE reviews SET photo=? WHERE id=?", (name, rid))
    except Exception as e:
        log.warning("Фото к отзыву не сохранилось: %s", e)
        await message.answer("Не получилось сохранить фото 😔 Попробуйте ещё раз или нажмите «Готово».",
                             reply_markup=kb([[btn("Готово", "rv_done")]]))
        return
    if message.caption:
        db.ex("UPDATE reviews SET text=? WHERE id=? AND text=''", (message.caption.strip()[:1000], rid))
    await finish(message, state)


@router.message(ReviewStates.text, F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/"))
async def got_text(message: Message, state: FSMContext):
    rid = await _review_id(state)
    if not rid:
        await state.clear()
        return
    db.ex("UPDATE reviews SET text=? WHERE id=?", (message.text.strip()[:1000], rid))
    await state.set_state(ReviewStates.photo)
    await message.answer("Спасибо! 🙏 Хотите добавить фото результата? Просто пришлите его сюда 📸",
                         reply_markup=kb([[btn("Готово, без фото", "rv_done")]]))


@router.message(ReviewStates.photo, F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/"))
async def photo_expected(message: Message, state: FSMContext):
    await message.answer("Пришлите фото 📸 или нажмите «Готово».", reply_markup=kb([[btn("Готово, без фото", "rv_done")]]))


@router.callback_query(F.data.in_({"rv_skip", "rv_done"}))
async def skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if await _review_id(state):
        await finish(cb.message, state)


async def finish(message: Message, state: FSMContext):
    rid = await _review_id(state)
    await state.clear()
    r = db.get_review(rid) if rid else None
    if not r:
        return
    await message.answer("Ваш отзыв опубликован 💛 Спасибо, что помогаете нам становиться лучше!", reply_markup=MENU)
    await notify_staff(r)


async def notify_staff(r: dict):
    """Новый отзыв — мастеру и владельцам, с кнопкой «Ответить»."""
    m = db.get_master(r["master_id"])
    chats = [m["tg_id"]] if m and m["tg_id"] else []
    chats += [a["tg_id"] for a in db.admins() if a["tg_id"] not in chats]
    text = (f"⭐ <b>Новый отзыв</b> о мастере {html.escape(r['master_name'])}\n"
            f"{stars_line(r['rating'])}  {html.escape(db.review_author(r))}"
            + (f" · {html.escape(r['service_name'])}" if r.get("service_name") else "")
            + (f"\n\n«{html.escape(r['text'])}»" if r["text"] else "")
            + (f"\n\n📸 Фото: {photo_url(r['photo'])}" if r["photo"] and photo_url(r["photo"]) else ""))
    from .applog import events
    events.info("⭐ Новый отзыв: %s, оценка %s", r["master_name"], r["rating"])
    for chat in chats:
        await notify.send(chat, text, kb([[btn("💬 Ответить на отзыв", ReplyTo(v=r["id"]))]]))


# ======================================================================
#  3. Мастер / владелец отвечает на отзыв
# ======================================================================
def can_reply(uid: int, r: dict) -> bool:
    if db.is_admin(uid):
        return True
    me = db.master_by_tg(uid)
    return bool(me and me["id"] == r["master_id"])


@router.callback_query(ReplyTo.filter())
async def reply_start(cb: CallbackQuery, callback_data: ReplyTo, state: FSMContext):
    r = db.get_review(callback_data.v)
    if not r or not can_reply(cb.from_user.id, r):
        await cb.answer("Отзыв не найден", show_alert=True)
        return
    await cb.answer()
    await state.set_state(ReplyStates.text)
    await state.update_data(reply_to=r["id"])
    await cb.message.answer("✍️ Напишите ответ на отзыв одним сообщением.\n"
                            "Его увидят все клиенты под отзывом, а автор получит его в мессенджер."
                            + (f"\n\nСейчас ответ: «{html.escape(r['reply'])}» — новый заменит его." if r["reply"] else ""),
                            reply_markup=kb([[btn("Отмена", "rr_cancel")]]))


@router.callback_query(F.data == "rr_cancel")
async def reply_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await safe_edit(cb.message, "Хорошо, ответ не отправлен.")


async def save_reply(r: dict, text: str, who: str) -> bool:
    db.ex("UPDATE reviews SET reply=?, reply_by=?, reply_at=? WHERE id=?", (text[:1000], who, db.now_str(), r["id"]))
    c = db.one("SELECT tg_id FROM clients WHERE id=?", (r["client_id"],))
    if c and c["tg_id"]:
        return await notify.send(c["tg_id"], f"💬 {html.escape(who)} ответил(а) на ваш отзыв:\n\n«{html.escape(text)}»")
    return False


@router.message(ReplyStates.text, F.text & ~F.text.startswith("/"))
async def reply_text(message: Message, state: FSMContext):
    rid = (await state.get_data()).get("reply_to")
    await state.clear()
    r = db.get_review(rid) if rid else None
    if not r or not can_reply(message.from_user.id, r):
        return
    me = db.master_by_tg(message.from_user.id)
    who = me["name"] if (me and me["id"] == r["master_id"]) else "Салон"
    await save_reply(r, message.text.strip(), who)
    await message.answer("✅ Ответ опубликован под отзывом.")


# ======================================================================
#  4. Клиенты читают отзывы
# ======================================================================
def masters_with_reviews() -> list[dict]:
    rt = db.master_ratings()
    return [dict(m, avg=rt[m["id"]][0], n=rt[m["id"]][1]) for m in db.masters() if m["id"] in rt]


async def show_masters(message: Message, edit: bool, uid: int | None = None):
    ms = masters_with_reviews()
    if not ms:
        text = "Отзывов пока нет 🙂 Запишитесь — и станьте первым, кто его оставит!"
        rows = [[btn("Записаться", "book")]]
    else:
        text = "⭐ <b>Отзывы о мастерах</b>\nНажмите на мастера, чтобы почитать отзывы 👇"
        rows = [[btn(f"{m['name']} · {db.stars(m['avg'], m['n'])}", ShowRev(m=m["id"]))] for m in ms]
    if uid and db.reviewable_visits(uid):
        rows.insert(0, [btn("✍️ Оставить отзыв о визите", "rv_write")])
    await (safe_edit(message, text, kb(rows)) if edit else message.answer(text, reply_markup=kb(rows)))


@router.message(F.text == B_REVIEWS)
async def menu_reviews(message: Message, state: FSMContext):
    await state.clear()
    await show_masters(message, edit=False, uid=message.from_user.id)


@router.callback_query(F.data == "rv_write")
async def write_review(cb: CallbackQuery):
    """Клиент сам решил оставить отзыв — выбираем, о каком визите."""
    visits = db.reviewable_visits(cb.from_user.id)
    await cb.answer()
    if not visits:
        await safe_edit(cb.message, "Недавних визитов без отзыва нет 🙂")
        return
    if len(visits) == 1:
        await ask_stars(cb.message, visits[0], edit=True)
        return
    rows = [[btn(f"{start_human(b['start'])} · {b['master_name']}", f"rv_pick:{b['id']}")] for b in visits]
    await safe_edit(cb.message, "О каком визите хотите оставить отзыв? 👇", kb(rows))


@router.callback_query(F.data.startswith("rv_pick:"))
async def pick_visit(cb: CallbackQuery):
    b = db.get_booking(int(cb.data.split(":")[1]))
    await cb.answer()
    if b and b["client_tg"] == cb.from_user.id:
        await ask_stars(cb.message, b, edit=True)


async def ask_stars(message: Message, b: dict, edit: bool):
    rows = [[btn(f"{'⭐' * r} {RATING_WORDS[r]}", Rate(b=b["id"], r=r))] for r in (5, 4, 3, 2, 1)]
    text = (f"Как вам визит к мастеру <b>{html.escape(b['master_name'])}</b>?\n"
            f"{html.escape(b['service_name'])} · {start_human(b['start'])}")
    await (safe_edit(message, text, kb(rows)) if edit else message.answer(text, reply_markup=kb(rows)))


def review_block(r: dict) -> str:
    when = start_human(r["start"]).split(" в ")[0] if r.get("start") else r["created_at"][:10]
    out = f"{stars_line(r['rating'])}  <b>{html.escape(db.review_author(r))}</b> · {when}"
    if r.get("service_name"):
        out += f"\n<i>{html.escape(r['service_name'])}</i>"
    if r["text"]:
        out += f"\n{html.escape(r['text'])}"
    if r["photo"] and photo_url(r["photo"]):
        out += f"\n📸 <a href=\"{photo_url(r['photo'])}\">Фото результата</a>"
    if r["reply"]:
        out += f"\n💬 <b>{html.escape(r['reply_by'] or 'Салон')}:</b> {html.escape(r['reply'])}"
    return out


@router.callback_query(ShowRev.filter())
async def show_reviews(cb: CallbackQuery, callback_data: ShowRev):
    await cb.answer()
    if not callback_data.m:
        await show_masters(cb.message, edit=True, uid=cb.from_user.id)
        return
    m = db.get_master(callback_data.m)
    rows = db.q(db.REVIEW_SQL + " WHERE r.master_id=? AND r.hidden=0 ORDER BY r.id DESC LIMIT ? OFFSET ?",
                (callback_data.m, PAGE + 1, callback_data.o))
    more = len(rows) > PAGE
    avg, n = db.master_ratings().get(callback_data.m, (0, 0))
    text = (f"⭐ <b>{html.escape(m['name'] if m else 'Мастер')}</b> — {db.stars(avg, n) or 'отзывов пока нет'}\n\n"
            + "\n\n".join(review_block(r) for r in rows[:PAGE]))
    buttons = []
    if more:
        buttons.append([btn("Ещё отзывы →", ShowRev(m=callback_data.m, o=callback_data.o + PAGE))])
    buttons.append([btn("← Все мастера", ShowRev(m=0)), btn("Записаться", "book")])
    await safe_edit(cb.message, text, kb(buttons))


# ---- отзыв «позже»: клиент сам пишет после визита — это уйдёт администратору как обычное сообщение ----
def rating_suffix(master_id: int) -> str:
    avg, n = db.master_ratings().get(master_id, (0, 0))
    return f" · {db.stars(avg, n)}" if n else ""


__all__ = ["router", "send_requests", "rating_suffix", "save_reply", "full_name"]
