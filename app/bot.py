"""Бот салона — один сценарий для Telegram и MAX (MAX подключается через app/maxbot.py).

Клиентская часть сделана для людей, которые «вообще не разбираются в технике»:
• внизу экрана всегда есть меню из больших кнопок (Записаться / Мои записи / Контакты / Помощь);
• каждый шаг подписан: «Шаг 2 из 4 — выберите мастера 👇», и видно, что уже выбрано;
• на каждом экране есть «← Назад» и «🏠 В начало»;
• если клиент просто пишет текст — бот не молчит, а передаёт сообщение администратору.
"""
import html
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo)

from . import config, db, notify, slots
from .config import runtime
from .notify import CCancel, MCancel, Reply
from .niche import N
from .texts import day_button, day_human, duration_human, money, price, start_human

log = logging.getLogger("bot")
router = Router()


# ---------- данные кнопок (m=0 — «любой мастер») ----------
class Svc(CallbackData, prefix="s"):
    s: int


class Mst(CallbackData, prefix="m"):
    s: int
    m: int


class Day(CallbackData, prefix="d"):
    s: int
    m: int
    d: str  # ГГГГММДД


class Tm(CallbackData, prefix="t"):
    s: int
    m: int
    d: str
    t: str  # ЧЧММ


class Ok(CallbackData, prefix="o"):
    s: int
    m: int
    d: str
    t: str


class Move(CallbackData, prefix="mv"):  # перенести запись
    b: int


class BookStates(StatesGroup):
    phone = State()


class ChatStates(StatesGroup):  # владелец/директор переписывается с клиенткой
    talking = State()


# ---------- кнопки нижнего меню (всегда на экране у клиента) ----------
B_BOOK = f"{N['emoji']} Записаться"
B_MY = "📋 Мои записи"
B_CONTACTS = "📍 Адрес и телефон"
B_HELP = "❓ Как записаться"
B_REVIEWS = "⭐ Отзывы"
B_SEND_PHONE = "📱 Отправить мой номер"
B_STOP = "↩️ Не записываться"
MENU_TEXTS = {B_BOOK, B_MY, B_CONTACTS, B_HELP, B_STOP, B_REVIEWS}

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_BOOK)], [KeyboardButton(text=B_MY), KeyboardButton(text=B_CONTACTS)],
              [KeyboardButton(text=B_REVIEWS), KeyboardButton(text=B_HELP)]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Нажмите кнопку внизу 👇")

PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_SEND_PHONE, request_contact=True)], [KeyboardButton(text=B_STOP)]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Или напишите номер: +7 900 123-45-67")


def below(chat_id: int) -> str:
    """Где кнопки: в Telegram — постоянное меню внизу экрана, в MAX (id < 0) — под сообщением."""
    return "под этим сообщением" if chat_id < 0 else "внизу экрана"


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data if isinstance(data, str) else data.pack())


def nav(back=None) -> list:
    """Нижний ряд на каждом шаге: Назад + В начало."""
    row = [btn("← Назад", back)] if back is not None else []
    return [row + [btn("🏠 В начало", "home")]]


def parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def full_name(u) -> str:
    return " ".join(filter(None, [u.first_name, u.last_name])) or "Клиент"


def salon() -> dict:
    return db.settings()


def is_staff(uid: int) -> bool:
    return db.is_admin(uid) or db.master_by_tg(uid) is not None


async def safe_edit(message: Message, text: str, markup=None):
    """Изменить сообщение; если не получилось (слишком старое и т.п.) — отправить новое."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


async def setup_commands(bot: Bot):
    await bot.set_my_commands([BotCommand(command="start", description="🏠 Главное меню"),
                               BotCommand(command="book", description=B_BOOK),
                               BotCommand(command="my", description="📋 Мои записи"),
                               BotCommand(command="help", description="❓ Как записаться")])


# ======================================================================
#  СОТРУДНИКИ: владелец и мастера
# ======================================================================
def cabinet_button() -> list:
    if not runtime["webapp_url"]:
        return []
    return [[InlineKeyboardButton(text="🌸 Открыть кабинет", web_app=WebAppInfo(url=runtime["webapp_url"]))]]


async def set_menu_button(bot: Bot, chat_id: int):
    if not runtime["webapp_url"]:
        return
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonWebApp(
            text="Кабинет", web_app=WebAppInfo(url=runtime["webapp_url"])))
    except Exception as e:
        log.warning("menu button %s: %s", chat_id, e)


async def refresh_staff_buttons(bot: Bot):
    for a in db.admins():
        await set_menu_button(bot, a["tg_id"])
    for m in db.masters():
        if m["tg_id"]:
            await set_menu_button(bot, m["tg_id"])


support_url = config.support_url


def support_row(role: str) -> list:
    url = support_url(role)
    return [[InlineKeyboardButton(text="🆘 Поддержка", url=url)]] if url else []


NO_CABINET = "\n\n<i>Кабинет сейчас недоступен: не запущен туннель. Запись клиентов при этом работает.</i>"


async def show_admin_home(message: Message, tg_id: int):
    await set_menu_button(message.bot, tg_id)
    s = salon()
    if db.is_solo():
        await message.answer(
            f"{N['emoji']} Ваш кабинет «{s['salon_name']}».\n\n"
            "Здесь ваши записи, клиенты, отзывы, прайс и график. Новые записи и сообщения клиентов приходят сюда же."
            f"{'' if runtime['webapp_url'] else NO_CABINET}",
            reply_markup=kb(cabinet_button() + [[btn("📅 Записи на сегодня и завтра", "today")],
                                                [btn("🔗 Ссылка для клиентов", "link")],
                                                [btn("❓ Как это работает", "help_staff")]] + support_row("owner")))
        return
    warn = "" if db.masters() else ("\n\n⚠️ <b>Первый шаг:</b> откройте кабинет → «Мастера» и добавьте мастеров. "
                                    "Пока мастеров нет, клиенты не смогут записаться.")
    await message.answer(
        f"🌸 Кабинет салона «{s['salon_name']}».{warn}{'' if runtime['webapp_url'] else NO_CABINET}",
        reply_markup=kb(cabinet_button() + [[btn("📅 Записи на сегодня и завтра", "today")],
                                            [btn("🔗 Ссылка для клиентов", "link")],
                                            [btn("❓ Как это работает", "help_staff")]] + support_row("owner")))


async def show_master_home(message: Message, master: dict):
    await set_menu_button(message.bot, master["tg_id"])
    await message.answer(
        f"🌸 {master['name']}, это ваш рабочий кабинет в «{salon()['salon_name']}».\n\n"
        f"Сюда будут приходить ваши новые записи и отмены, а вечером — план на завтра."
        f"{'' if runtime['webapp_url'] else NO_CABINET}",
        reply_markup=kb(cabinet_button() + [[btn("📅 Мои записи на сегодня и завтра", "today")]] + support_row("master")))


DEV_MODE_NAMES = {"client": "👤 Клиент", "owner": "👑 Хозяин"}


async def dev_enter(message: Message, state: FSMContext):
    """Техподдержка пришла из админ-бота: показать меню той роли, которую она выбрала."""
    from aiogram.types import MenuButtonDefault, ReplyKeyboardRemove
    uid = message.from_user.id
    mode = db.dev_mode(uid) or ""
    if mode.startswith("master:"):
        m = db.master_by_tg(uid)
        title = f"✂️ Мастер {m['name']}" if m else "✂️ Мастер (не найден — выберите другого в админ-боте)"
    else:
        title = DEV_MODE_NAMES.get(mode, "обычный")
    note = f"🛠 Режим техподдержки: <b>{html.escape(title)}</b> в «{html.escape(salon()['salon_name'])}»"
    if mode == "owner" or mode.startswith("master:"):
        await message.answer(note, reply_markup=ReplyKeyboardRemove())
    else:
        try:  # клиенту кнопка «Кабинет» не положена
            await message.bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonDefault())
        except Exception:
            pass
        await message.answer(note)
    await start_plain(message, state)


@router.message(CommandStart(deep_link=True))
async def start_deep(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if command.args == "dev" and message.from_user.id == db.dev_id():
        await dev_enter(message, state)
        return
    o = re.fullmatch(r"owner_([0-9a-f]+)", command.args or "")
    if o:
        st = db.settings()
        if st.get("owner_code") and o.group(1) == st["owner_code"] and not db.dev_mode(message.from_user.id):
            await become_owner(message, message.from_user, st.get("owner_mode") == "solo")
        else:
            await message.answer("Эта ссылка владельца уже использована или устарела. Напишите разработчику — он пришлёт новую.")
        return
    a = re.fullmatch(r"admin_([0-9a-f]+)", command.args or "")
    if a and db.admins() and a.group(1) == db.settings().get("admin_invite"):
        db.add_admin(message.from_user.id, full_name(message.from_user))
        db.set_setting("admin_invite", "")  # ссылка одноразовая
        from .applog import events
        events.info("🔑 Новый администратор: %s", full_name(message.from_user))
        await message.answer("✅ Вы подключены как администратор салона: видите все записи и отвечаете клиенткам.")
        await show_admin_home(message, message.from_user.id)
        for adm in db.admins():
            if adm["tg_id"] != message.from_user.id:
                await notify.send(adm["tg_id"], f"✅ {full_name(message.from_user)} подключён(а) как администратор.")
        return
    m = re.fullmatch(r"staff_([0-9a-f]+)", command.args or "")
    if not m:
        await start_plain(message, state)
        return
    master = db.one("SELECT * FROM masters WHERE invite=? AND active=1", (m.group(1),))
    if not master:
        await message.answer("Ссылка-приглашение устарела. Попросите у владельца салона новую.")
        return
    if master["tg_id"] and master["tg_id"] != message.from_user.id:
        await message.answer("По этой ссылке уже подключился другой человек. Попросите у владельца новую ссылку.")
        return
    db.ex("UPDATE masters SET tg_id=? WHERE id=?", (message.from_user.id, master["id"]))
    master = db.get_master(master["id"])
    await message.answer("✅ Готово, вы подключены как мастер!")
    from .applog import events
    events.info("👩 Мастер %s подключился (%s)", master["name"], "MAX" if message.from_user.id < 0 else "Telegram")
    await show_master_home(message, master)
    for a in db.admins():
        await notify.send(a["tg_id"], f"✅ Мастер {master['name']} подключил(а) мессенджер — "
                                      f"теперь он(а) получает уведомления о своих записях.")


@router.message(CommandStart())
async def start_plain(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if db.is_admin(uid):
        await show_admin_home(message, uid)
        return
    if is_reserved_owner(message.from_user) and not db.dev_mode(uid):  # заказчик, которого назначил разработчик
        await become_owner(message, message.from_user, db.settings().get("owner_mode") == "solo")
        return
    master = db.master_by_tg(uid)
    if master:
        await show_master_home(message, master)
        return
    if not db.admins() and not db.dev_mode(uid) and not owner_reserved():  # техподдержку подключать салон не просим
        await message.answer(
            f"Здравствуйте! 👋 Это ваша онлайн-запись в Telegram и MAX.\n\n"
            "📲 Клиенты записываются сами — круглосуточно\n"
            "⏰ Бот напоминает им о визите\n"
            "⭐ Собирает отзывы с фото\n"
            "📊 Записи, клиенты и выручка — в кабинете прямо здесь\n\n"
            "<b>Как вы работаете?</b> От этого зависит, каким будет ваш кабинет 👇",
            reply_markup=kb([[btn(f"{N['emoji']} Я мастер — работаю на себя", "reg:solo")],
                             [btn("🏠 У меня салон с мастерами", "reg:salon")]]))
        return
    await client_welcome(message)


@router.callback_query(F.data.in_({"reg", "reg:solo", "reg:salon"}))
async def register(cb: CallbackQuery):
    uid = cb.from_user.id
    if db.dev_mode(uid):
        await cb.answer("В режиме техподдержки салон не подключают — это делает владелец", show_alert=True)
        return
    if (db.admins() and not db.is_admin(uid)) or owner_reserved():
        await cb.answer("Уже подключено", show_alert=True)
        return
    await cb.answer("Готово!")
    await become_owner(cb.message, cb.from_user, cb.data == "reg:solo")


def owner_reserved() -> bool:
    """Разработчик заранее назначил владельца (по @username или личной ссылке) — чужим кнопку «подключить» не показываем."""
    s = db.settings()
    return bool(s.get("owner_username") or s.get("owner_code"))


def is_reserved_owner(user) -> bool:
    want = (db.settings().get("owner_username") or "").lstrip("@").lower()
    return bool(want) and (user.username or "").lower() == want


async def become_owner(message: Message, user, solo: bool):
    """Человек становится владельцем: формат «частный мастер» (solo) или «салон с мастерами»."""
    uid = user.id
    db.add_admin(uid, full_name(user))
    db.set_setting("mode", "solo" if solo else "salon")
    for k in ("owner_username", "owner_code", "owner_mode"):
        db.set_setting(k, "")  # назначение выполнено — ссылка и @username больше не действуют
    from .applog import events
    if solo:
        if not db.own_master(uid):
            ms = db.masters()
            if len(ms) == 1 and not ms[0]["tg_id"]:  # салон подготовили заранее: мастер с услугами уже есть
                db.ex("UPDATE masters SET tg_id=? WHERE id=?", (uid, ms[0]["id"]))
            else:
                db.create_master(full_name(user), "", uid)  # частный мастер = владелец и единственный мастер
        events.info("👑 Владелец подключился (частный мастер): %s", full_name(user))
        await message.answer(
            "🎉 Готово! Ваша запись работает.\n\n"
            "<b>Осталось 3 шага — 5 минут:</b>\n"
            "1. Откройте <b>кабинет</b> → «Профиль»: название, адрес, телефон.\n"
            "2. Там же — «Услуги и цены»: " + ("поправьте стартовый прайс под себя.\n" if db.services() else "добавьте свои услуги.\n") +
            "3. Вкладка «График» — дни и часы, когда принимаете.\n\n"
            "Потом поставьте ссылку для записи в профиль Telegram, Instagram, VK, на Яндекс Карты и в 2ГИС — "
            "кнопка «🔗 Ссылка для клиентов» ниже.")
    else:
        events.info("👑 Владелец подключился (салон): %s", full_name(user))
        await message.answer(
            "🎉 Салон подключён, вы — владелец.\n\n"
            "Что сделать дальше:\n"
            "1. Откройте <b>кабинет</b> → «Салон»: название, адрес, телефон.\n"
            "2. «Мастера» → добавьте мастеров, отметьте их услуги и график.\n"
            "3. Каждому мастеру отправьте его ссылку-приглашение — так он будет получать свои записи.\n"
            "4. «Салон» → «Услуги и цены» — " + ("поправьте стартовый прайс под себя.\n" if db.services() else "добавьте свои услуги.\n") +
            "5. Ссылку на бота поставьте в профиль салона, VK, на Яндекс Карты и в 2ГИС.",
            reply_markup=kb([[btn(f"{N['emoji']} Я тоже мастер — добавить себя", "self_master")]]))
    await show_admin_home(message, uid)


@router.callback_query(F.data == "self_master")
async def self_master(cb: CallbackQuery):
    if not db.is_admin(cb.from_user.id):
        await cb.answer()
        return
    if db.master_by_tg(cb.from_user.id):
        await cb.answer("Вы уже в списке мастеров", show_alert=True)
        return
    m = db.create_master(full_name(cb.from_user), "", cb.from_user.id)
    await cb.answer("Готово!")
    await cb.message.answer(f"{N['emoji']} Вы добавлены мастером: {m['name']}. Услуги и график — в кабинете → «Мастера».")


@router.callback_query(F.data == "link")
async def send_link(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        f"Ссылка для записи в «{salon()['salon_name']}»:\nhttps://t.me/{runtime['bot_username']}\n"
        + (f"В MAX: https://max.ru/{runtime['max_username']}\n" if runtime.get("max_username") else "") + "\n"
        "Перешлите это сообщение клиентам, поставьте ссылку в профиль салона, VK, на Яндекс Карты и в 2ГИС.",
        disable_web_page_preview=True)


@router.callback_query(F.data == "help_staff")
async def help_staff(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "<b>Как это работает</b>\n\n"
        "1. Клиент открывает бота — внизу у него всегда меню: «Записаться», «Мои записи», «Адрес и телефон».\n"
        "2. Запись по шагам: услуга → мастер (или «любой свободный») → день → время → телефон.\n"
        "3. Мастеру и вам приходит уведомление о записи. Клиент сам может отменить или перенести запись.\n"
        "4. Клиент получает напоминания за сутки и за 2 часа.\n"
        "5. В 20:00 каждый мастер получает свой план на завтра, а вы — план всего салона.\n"
        "6. Если клиентка пишет вопрос или присылает фото — оно придёт сюда с кнопкой «💬 Ответить». "
        "Нажмите её — включится режим переписки: всё, что вы напишете, получит клиентка. "
        "Ещё можно просто ответить на её сообщение свайпом. Все чаты есть и в кабинете → «Чаты».\n"
        "7. В кабинете: записи, мастера, услуги, клиенты, настройки салона.")


@router.callback_query(F.data == "today")
async def today_cb(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    master = None if db.is_admin(uid) else db.master_by_tg(uid)
    if not master and not db.is_admin(uid):
        return
    today = db.now_local().date()
    parts = []
    for d in (today, today + timedelta(days=1)):
        sql = db.BOOKING_SQL + " WHERE b.status='active' AND b.start LIKE ?"
        params = [d.isoformat() + "%"]
        if master:
            sql += " AND b.master_id=?"
            params.append(master["id"])
        rows = db.q(sql + " ORDER BY b.start", params)
        lines = [f"{r['start'][11:]} — {r['client_name']}, {r['service_name']}"
                 + ("" if master else f" · {r['master_name']}") for r in rows]
        parts.append(f"<b>{day_human(d).capitalize()}</b>\n" + ("\n".join(lines) if lines else "записей нет"))
    await cb.message.answer("\n\n".join(parts))


# ======================================================================
#  КЛИЕНТЫ
# ======================================================================
def salon_head() -> str:
    s = salon()
    text = f"🌸 <b>{s['salon_name']}</b>"
    if s.get("about"):
        text += f"\n{s['about']}"
    return text


async def client_welcome(message: Message):
    """Первое сообщение клиенту: приветствие + нижнее меню + большая кнопка «Записаться»."""
    await message.answer(
        f"{salon_head()}\n\nЗдравствуйте! 👋 Я помогу записаться на {N['service_acc']} — это займёт меньше минуты.\n\n"
        + ("Кнопки меню — под этим сообщением 👇\n" if message.chat.id < 0 else "Внизу экрана у вас всегда есть меню с кнопками 👇\n") +
        f"• <b>{B_BOOK}</b> — новая запись\n"
        f"• <b>{B_MY}</b> — посмотреть, перенести или отменить\n"
        f"• <b>{B_CONTACTS}</b> — как нас найти\n"
        f"• <b>{B_REVIEWS}</b> — что пишут о мастерах",
        reply_markup=MENU)
    await message.answer("Начнём? 👇", reply_markup=kb([[btn(f"{N['emoji']} Записаться на {N['service_acc']}", "book")]]))


# ---- общие тексты шагов ----
def step_head(step: int, total: int, what: str) -> str:
    return f"<b>Шаг {step} из {total}.</b> {what} 👇"


def chosen(s: dict | None = None, m: int | None = None, d: date | None = None, t: str | None = None) -> str:
    """Что клиент уже выбрал — показываем на каждом шаге."""
    lines = []
    if s:
        lines.append(f"{N['emoji']} {s['name']} · {price(s)} · {duration_human(s['duration'])}")
    if m is not None:
        lines.append(f"👩 Мастер: {master_label(m)}")
    if d:
        lines.append(f"🗓 {day_human(d).capitalize()}" + (f" в {t}" if t else ""))
    return ("Вы выбрали:\n" + "\n".join(lines) + "\n\n") if lines else ""


def master_label(m: int) -> str:
    if not m:
        return "любой свободный"
    master = db.get_master(m)
    return master["name"] if master else "—"


def has_master_step(service_id: int) -> bool:
    return len(db.masters_for_service(service_id)) > 1


def steps_total(service_id: int | None) -> int:
    if service_id is None:
        return 4 if len(db.masters()) > 1 else 3
    return 4 if has_master_step(service_id) else 3


# ---- нижнее меню работает на любом шаге ----
@router.message(F.text == B_BOOK)
@router.message(Command("book"))
async def menu_book(message: Message, state: FSMContext):
    await state.clear()
    await show_services(message, edit=False)


@router.message(F.text == B_MY)
@router.message(Command("my"))
async def menu_my(message: Message, state: FSMContext):
    await state.clear()
    await show_my(message, message.from_user.id)


@router.message(F.text == B_CONTACTS)
async def menu_contacts(message: Message, state: FSMContext):
    await state.clear()
    s = salon()
    lines = [f"🌸 <b>{s['salon_name']}</b>"]
    if s.get("address"):
        lines.append(f"📍 {s['address']}")
    if s.get("phone"):
        lines.append(f"📞 {s['phone']}")
    if len(lines) == 1:
        lines.append("Салон пока не указал адрес и телефон.")
    rows = []
    if s.get("address"):
        rows.append([InlineKeyboardButton(text="🗺 Открыть на карте",
                                          url=f"https://yandex.ru/maps/?text={quote(s['address'])}")])
    rows.append([btn(B_BOOK, "book")])
    await message.answer("\n".join(lines), reply_markup=kb(rows))


@router.message(F.text == B_HELP)
@router.message(Command("help"))
async def menu_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Как записаться — очень просто:</b>\n\n"
        f"1️⃣ Нажмите <b>{B_BOOK}</b> {below(message.chat.id)}.\n"
        "2️⃣ Нажмите на нужную услугу.\n"
        "3️⃣ Выберите мастера — или «Любой свободный мастер».\n"
        "4️⃣ Нажмите на удобный день, потом на время.\n"
        "5️⃣ Нажмите «📱 Отправить мой номер» — это нужно один раз.\n\n"
        "Готово! Я пришлю напоминание за день и за 2 часа до визита. 💌\n\n"
        f"Передумали или хотите другое время? Нажмите <b>{B_MY}</b> — там можно перенести или отменить.\n\n"
        "Ошиблись кнопкой? Нажмите «← Назад» или «🏠 В начало». Ничего не сломается 🙂\n\n"
        "Остались вопросы — просто напишите их сюда сообщением, я передам администратору."
        + ("\n\nЧто-то не работает в самом боте? Нажмите «🆘 Поддержка»." if support_url("client") else ""),
        reply_markup=kb(support_row("client")) if support_url("client") else MENU)


@router.message(F.text == B_STOP)
async def menu_stop(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(f"Хорошо, запись не создана. Меню — {below(message.chat.id)} 👇", reply_markup=MENU)


@router.callback_query(F.data == "home")
async def home_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await safe_edit(cb.message, f"{salon_head()}\n\nЧто хотите сделать? 👇",
                    kb([[btn(B_BOOK, "book")], [btn("📋 Мои записи", "my")]]))


@router.callback_query(F.data == "x")
async def nothing(cb: CallbackQuery):
    """Заголовки «Утро / День / Вечер» — нажатие ничего не делает."""
    await cb.answer("Нажмите на время ниже 👇")


# ---- шаг 1: услуга ----
async def show_services(message: Message, edit: bool = True):
    services = db.bookable_services()
    if not services:
        text = f"{salon_head()}\n\nОнлайн-запись скоро откроется 🙏 Пока можно позвонить в салон — кнопка «{B_CONTACTS}»."
        await (safe_edit(message, text, kb(nav())) if edit else message.answer(text, reply_markup=MENU))
        return
    rows = [[btn(f"{s['name']} · {price(s)}", Svc(s=s["id"]))] for s in services] + nav()
    text = step_head(1, steps_total(None), "Нажмите на услугу, на которую хотите записаться")
    await (safe_edit(message, text, kb(rows)) if edit else message.answer(text, reply_markup=kb(rows)))


@router.callback_query(F.data == "book")
async def book_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(move_from=None)
    await show_services(cb.message)


# ---- шаг 2: мастер ----
@router.callback_query(Svc.filter())
async def choose_service(cb: CallbackQuery, callback_data: Svc):
    s = db.get_service(callback_data.s)
    masters = db.masters_for_service(callback_data.s) if s and s["active"] else []
    if not masters:
        await cb.answer("Эту услугу сейчас нельзя забронировать", show_alert=True)
        return
    if len(masters) == 1:
        await cb.answer()
        await show_days(cb.message, Mst(s=s["id"], m=masters[0]["id"]))
        return
    await cb.answer()
    rows = [[btn("✨ Любой свободный мастер", Mst(s=s["id"], m=0))]]
    rt = db.master_ratings()
    rows += [[btn(m["name"] + (f" · {db.stars(*rt[m['id']])}" if m["id"] in rt else (f" · {m['spec']}" if m["spec"] else "")),
                  Mst(s=s["id"], m=m["id"]))] for m in masters]
    rows += nav("book")
    await safe_edit(cb.message, chosen(s) + step_head(2, 4, "Выберите мастера.\n"
                    "Если всё равно к кому — нажмите «Любой свободный мастер», так больше свободного времени"),
                    kb(rows))


# ---- шаг 3: день ----
@router.callback_query(Mst.filter())
async def choose_master(cb: CallbackQuery, callback_data: Mst):
    await cb.answer()
    await show_days(cb.message, callback_data)


async def show_days(message: Message, callback_data: Mst, edit: bool = True):
    s = db.get_service(callback_data.s)
    days = slots.available_days(s, callback_data.m)
    multi = has_master_step(s["id"])
    back = Svc(s=s["id"]) if multi else "book"
    total = 4 if multi else 3
    if not days:
        text = chosen(s, callback_data.m) + ("😔 Свободного времени в ближайшие две недели нет."
                                             + ("\nПопробуйте выбрать другого мастера ← Назад" if multi else ""))
        await (safe_edit(message, text, kb(nav(back))) if edit else message.answer(text, reply_markup=kb(nav(back))))
        return
    today = db.now_local().date()
    buttons = [btn(day_button(d, today), Day(s=s["id"], m=callback_data.m, d=d.strftime("%Y%m%d"))) for d in days]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + nav(back)
    text = chosen(s, callback_data.m) + step_head(total - 1, total, "Нажмите на удобный день")
    await (safe_edit(message, text, kb(rows)) if edit else message.answer(text, reply_markup=kb(rows)))


# ---- шаг 4: время ----
PARTS = [("☀️ Утро", 0, 12 * 60), ("🌤 День", 12 * 60, 17 * 60), ("🌙 Вечер", 17 * 60, 24 * 60)]


@router.callback_query(Day.filter())
async def choose_day(cb: CallbackQuery, callback_data: Day):
    s = db.get_service(callback_data.s)
    d = parse_day(callback_data.d)
    times = list(slots.slots_by_time(s, d, callback_data.m))
    await cb.answer()
    back = Mst(s=s["id"], m=callback_data.m)
    total = steps_total(s["id"])
    if not times:
        await safe_edit(cb.message, chosen(s, callback_data.m, d) + "На этот день время уже разобрали 😔 Выберите другой день ← Назад",
                        kb(nav(back)))
        return
    rows = []
    for title, a, b in PARTS:
        part = [t for t in times if a <= slots.to_min(t) < b]
        if not part:
            continue
        rows.append([btn(title, "x")])
        buttons = [btn(t, Tm(s=s["id"], m=callback_data.m, d=callback_data.d, t=t.replace(":", ""))) for t in part]
        rows += [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows += nav(back)
    await safe_edit(cb.message, chosen(s, callback_data.m, d) + step_head(total, total, "Нажмите на удобное время"),
                    kb(rows))


# ---- подтверждение / телефон ----
@router.callback_query(Tm.filter())
async def choose_time(cb: CallbackQuery, callback_data: Tm, state: FSMContext):
    s = db.get_service(callback_data.s)
    d = parse_day(callback_data.d)
    t = f"{callback_data.t[:2]}:{callback_data.t[2:]}"
    if not slots.pick_master(s, d, t, callback_data.m):
        await cb.answer("Это время только что заняли 😔 Выберите другое", show_alert=True)
        return
    await cb.answer()
    client = db.find_client(cb.from_user.id)
    if client and client["phone"]:
        await safe_edit(cb.message,
                        "Почти готово! Проверьте, всё ли верно:\n\n" + chosen(s, callback_data.m, d, t)
                        + (f"📍 {salon()['address']}\n\n" if salon().get("address") else "")
                        + "Если всё верно — нажмите зелёную галочку 👇",
                        kb([[btn("✅ Да, записаться", Ok(**callback_data.model_dump()))],
                            [btn("✏️ Выбрать другое время", Day(s=s["id"], m=callback_data.m, d=callback_data.d))],
                            [btn("🏠 В начало", "home")]]))
        return
    await state.set_state(BookStates.phone)
    await state.update_data(pending=callback_data.model_dump())
    await safe_edit(cb.message, chosen(s, callback_data.m, d, t) + "Осталось совсем чуть-чуть ✨")
    await cb.message.answer(
        "<b>Последний шаг — ваш номер телефона.</b>\n\n"
        f"Нажмите большую кнопку <b>«{B_SEND_PHONE}»</b> {below(cb.message.chat.id)} 👇\n"
        "Мессенджер спросит разрешение — нажмите «Поделиться» или «OK».\n\n"
        "Номер нужен, чтобы мастер мог связаться с вами, если что-то изменится. Мы не звоним с рекламой.",
        reply_markup=PHONE_KB)


PHONE_RE = re.compile(r"^\+?[\d\s\-()]{10,18}$")


@router.message(BookStates.phone, F.contact | F.text)
async def got_phone(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
        phone = phone if phone.startswith("+") else "+" + phone
    elif message.text and PHONE_RE.match(message.text.strip()):
        phone = message.text.strip()
    else:
        await message.answer(f"Не получилось распознать номер 🙈\n\nПроще всего — нажмите кнопку "
                             f"<b>«{B_SEND_PHONE}»</b> {below(message.chat.id)} 👇\nИли напишите номер цифрами, например: +7 900 123-45-67",
                             reply_markup=PHONE_KB)
        return
    data = await state.get_data()
    pending, move_from = data.get("pending"), data.get("move_from")
    await state.clear()
    if not pending:
        await message.answer("Что-то пошло не так, давайте начнём заново 🙏", reply_markup=MENU)
        return
    db.get_or_create_client(message.from_user.id, full_name(message.from_user), phone, message.from_user.username or "")
    await message.answer("Спасибо, номер сохранён ✅ Больше спрашивать не буду.", reply_markup=MENU)
    await finish_booking(message, message.from_user, Ok(**pending), move_from)


@router.callback_query(Ok.filter())
async def confirm(cb: CallbackQuery, callback_data: Ok, state: FSMContext):
    await cb.answer()
    move_from = (await state.get_data()).get("move_from")
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await finish_booking(cb.message, cb.from_user, callback_data, move_from)


async def finish_booking(message: Message, user, data: Ok, move_from: int | None = None):
    s = db.get_service(data.s)
    d = parse_day(data.d)
    t = f"{data.t[:2]}:{data.t[2:]}"
    mid = slots.pick_master(s, d, t, data.m) if s else None
    if not mid:
        await message.answer("😔 Пока вы выбирали, это время заняли. Давайте выберем другое:",
                             reply_markup=kb([[btn("🗓 Выбрать другое время", Mst(s=data.s, m=data.m))]]))
        return
    client = db.get_or_create_client(user.id, full_name(user), username=user.username or "")
    b = db.get_booking(db.create_booking(mid, client["id"], s, f"{d.isoformat()} {t}"))
    moved = None
    if move_from:
        old = db.get_booking(move_from)
        if old and old["client_tg"] == user.id and old["status"] == "active":
            db.cancel_booking(old["id"], "client")
            moved = old
    s_ = salon()
    await message.answer(
        ("🎉 <b>Запись перенесена!</b>" if moved else "🎉 <b>Вы записаны!</b>")
        + f"\n\n🌸 {s_['salon_name']}\n🗓 {start_human(b['start'])}\n{N['emoji']} {b['service_name']} · {price(s)}\n"
        f"👩 Мастер: {b['master_name']}"
        + (f"\n📍 {s_['address']}" if s_.get("address") else "")
        + "\n\n⏰ Я напомню за день и за 2 часа до визита."
        + f"\n📸 {N['photo_hint']}"
        + f"\n\nПеренести или отменить — кнопка <b>{B_MY}</b> {below(message.chat.id)} 👇",
        reply_markup=MENU)
    if moved:
        await notify.staff_cancelled_by_client(moved)
    await notify.staff_new_booking(b)


# ---- мои записи: перенос и отмена ----
async def show_my(message: Message, uid: int):
    items = db.client_future_bookings(uid)
    if not items:
        await message.answer("У вас пока нет записей 🌸\n\nЗаписаться — кнопка ниже 👇",
                             reply_markup=kb([[btn(B_BOOK, "book")]]))
        return
    await message.answer(f"Ваши записи ({len(items)}):" if len(items) > 1 else "Ваша запись:")
    for b in items:
        await message.answer(f"🗓 <b>{start_human(b['start'])}</b>\n{N['emoji']} {b['service_name']}\n👩 Мастер: {b['master_name']}",
                             reply_markup=kb([[btn("🔄 Перенести", Move(b=b["id"])), btn("❌ Отменить", CCancel(b=b["id"]))]]))


@router.callback_query(F.data == "my")
async def my_cb(cb: CallbackQuery):
    await cb.answer()
    await show_my(cb.message, cb.from_user.id)


@router.callback_query(Move.filter())
async def move_booking(cb: CallbackQuery, callback_data: Move, state: FSMContext):
    b = db.get_booking(callback_data.b)
    if not b or b["client_tg"] != cb.from_user.id or b["status"] != "active":
        await cb.answer("Эта запись уже неактуальна", show_alert=True)
        return
    s = db.get_service(b["service_id"])
    if not s or not s["active"] or not db.get_master(b["master_id"])["active"]:
        await cb.answer("Эту запись нельзя перенести онлайн — отмените её и запишитесь заново", show_alert=True)
        return
    await cb.answer()
    await state.update_data(move_from=b["id"])
    await cb.message.answer(f"🔄 Переносим запись <b>{start_human(b['start'])}</b>.\n\n"
                            "Выберите новый день и время. Старая запись отменится сама, "
                            "когда вы подтвердите новую — до этого она остаётся за вами.")
    await show_days(cb.message, Mst(s=s["id"], m=b["master_id"]), edit=False)


@router.callback_query(CCancel.filter())
async def client_cancel(cb: CallbackQuery, callback_data: CCancel):
    b = db.get_booking(callback_data.b)
    if not b or b["client_tg"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if b["status"] != "active":
        await cb.answer("Эта запись уже отменена")
        return
    if not callback_data.sure:
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=kb([
            [btn("Да, отменить запись", CCancel(b=b["id"], sure=1))],
            [btn("Нет, оставить", "keep")], [btn("🔄 Лучше перенести", Move(b=b["id"]))]]))
        return
    db.cancel_booking(b["id"], "client")
    await cb.answer("Запись отменена")
    await safe_edit(cb.message, f"❌ Запись отменена\n\n🗓 {start_human(b['start'])}\n{N['emoji']} {b['service_name']}\n\n"
                                f"Будем рады видеть вас в другой день 🌸",
                    kb([[btn(f"{N['emoji']} Записаться на другое время", "book")]]))
    await notify.staff_cancelled_by_client(b)


@router.callback_query(F.data == "keep")
async def keep_cb(cb: CallbackQuery):
    await cb.answer("Запись остаётся 👍")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---- сотрудник отменяет запись из уведомления ----
@router.callback_query(MCancel.filter())
async def staff_cancel(cb: CallbackQuery, callback_data: MCancel):
    b = db.get_booking(callback_data.b)
    me = db.master_by_tg(cb.from_user.id)
    allowed = b and (db.is_admin(cb.from_user.id) or b["master_tg"] == cb.from_user.id
                     or (me and me["id"] == b.get("master_id")))
    if not allowed:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if b["status"] != "active":
        await cb.answer("Запись уже отменена")
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    if not callback_data.sure:
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=kb([
            [btn("Да, отменить и сообщить клиенту", MCancel(b=b["id"], sure=1))], [btn("Нет", "noop")]]))
        return
    db.cancel_booking(b["id"], "salon")
    await cb.answer("Запись отменена, клиент получит уведомление")
    await cb.message.edit_text(cb.message.html_text + "\n\n<b>❌ Отменена</b>")
    await notify.client_cancelled_by_salon(b)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)



# ---- владелец/директор отвечает клиентке ----
B_END_CHAT = "✖️ Закончить ответ"
CHAT_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_END_CHAT)]], resize_keyboard=True, is_persistent=True,
                              input_field_placeholder="Сообщение для клиентки…")


def author_name(u) -> str:
    a = db.one("SELECT name FROM admins WHERE tg_id=?", (u.id,))
    return a["name"] if a else full_name(u)


def history_text(client_id: int, limit: int = 6) -> str:
    rows = db.q("SELECT * FROM messages WHERE client_id=? ORDER BY id DESC LIMIT ?", (client_id, limit))[::-1]
    lines = []
    for r in rows:
        who = "Клиентка" if r["direction"] == "in" else (r["author"] or "Салон")
        body = html.escape(r["text"]) if r["text"] else ""
        if r["photo"]:
            body = "📸 фото" + (f": {body}" if body else "")
        lines.append(f"<b>{who}</b> ({r['created_at'][11:16]}): {body}")
    return "\n".join(lines)


@router.callback_query(Reply.filter())
async def start_reply(cb: CallbackQuery, callback_data: Reply, state: FSMContext):
    if not db.is_admin(cb.from_user.id):
        await cb.answer("Отвечать клиенткам может владелец или администратор", show_alert=True)
        return
    client = db.one("SELECT * FROM clients WHERE id=?", (callback_data.c,))
    if not client or not client["tg_id"]:
        await cb.answer("Эта клиентка не подключена к боту", show_alert=True)
        return
    await cb.answer()
    await state.set_state(ChatStates.talking)
    await state.update_data(client_id=client["id"])
    db.ex("UPDATE messages SET is_read=1 WHERE client_id=? AND direction='in'", (client["id"],))
    hist = history_text(client["id"])
    await cb.message.answer(
        f"✍️ <b>Режим ответа: {html.escape(client['name'])}</b>"
        + (f" · {client['phone'] if not db.dev_mode(cb.from_user.id) else 'телефон скрыт'}" if client["phone"] else "")
        + (f"\n\nПоследние сообщения:\n{hist}" if hist else "")
        + "\n\nВсё, что вы сейчас напишете (текст или фото), получит клиентка от имени салона.\n"
          f"Когда закончите — нажмите «{B_END_CHAT}» {below(cb.message.chat.id)}.",
        reply_markup=CHAT_KB)


@router.message(ChatStates.talking, F.text == B_END_CHAT)
async def end_reply(message: Message, state: FSMContext):
    await state.clear()
    from aiogram.types import ReplyKeyboardRemove
    await message.answer("Режим ответа выключен ✅ Новые сообщения клиенток будут приходить сюда с кнопкой «💬 Ответить».",
                         reply_markup=ReplyKeyboardRemove())


@router.message(ChatStates.talking, F.text & ~F.text.startswith("/"))
async def talking_text(message: Message, state: FSMContext):
    cid = (await state.get_data()).get("client_id")
    client = db.one("SELECT * FROM clients WHERE id=?", (cid,)) if cid else None
    if not client:
        await state.clear()
        await message.answer("Клиентка не найдена, режим ответа выключен.")
        return
    ok = await notify.salon_reply(client, message.text, author_name(message.from_user))
    await message.answer("✓ Отправлено" if ok else "Не удалось отправить: клиентка могла заблокировать бота.",
                         reply_markup=CHAT_KB)


@router.message(ChatStates.talking, F.photo)
async def talking_photo(message: Message, state: FSMContext):
    cid = (await state.get_data()).get("client_id")
    client = db.one("SELECT * FROM clients WHERE id=?", (cid,)) if cid else None
    if not client:
        await state.clear()
        return
    try:
        from .maxbot import TG_PHOTOS  # чтобы фото можно было переслать и клиентке в MAX
        TG_PHOTOS[(message.chat.id, message.message_id)] = message.photo[-1].file_id
        await message.bot.copy_message(client["tg_id"], message.chat.id, message.message_id)
        db.log_message(client["id"], "out", message.caption or "", photo=message.photo[-1].file_id,
                       author=author_name(message.from_user))
        await message.answer("✓ Фото отправлено", reply_markup=CHAT_KB)
    except Exception:
        await message.answer("Не удалось отправить фото.", reply_markup=CHAT_KB)


@router.message(F.reply_to_message & F.text & ~F.text.startswith("/"))
async def swipe_reply(message: Message):
    """Владелец ответил свайпом на пересланное сообщение клиентки."""
    cid = db.relay_client(message.chat.id, message.reply_to_message.message_id)
    if not cid or not db.is_admin(message.from_user.id):
        await client_text(message)
        return
    client = db.one("SELECT * FROM clients WHERE id=?", (cid,))
    ok = await notify.salon_reply(client, message.text, author_name(message.from_user))
    await message.answer(f"✓ Отправлено: {html.escape(client['name'])}" if ok else "Не удалось отправить.")


# ---- фото и любые сообщения от клиента → администратору ----
def client_signature(uid: int, fallback_name: str, username: str | None) -> str:
    c = db.find_client(uid)
    name = c["name"] if c else fallback_name
    extra = [f"@{username}" if username else "", c["phone"] if c and c["phone"] else ""]
    extra = ", ".join(x for x in extra if x)
    return name + (f" ({extra})" if extra else "")


@router.message(F.photo)
async def client_photo(message: Message):
    uid = message.from_user.id
    if is_staff(uid):
        return
    upcoming = db.client_future_bookings(uid)
    if upcoming:
        b = upcoming[0]
        targets = notify.staff_chats(b)
        about = f"к записи {start_human(b['start'])} — {b['service_name']}, мастер {b['master_name']}"
    else:
        targets = [a["tg_id"] for a in db.admins()]
        about = "без записи"
    client = db.get_or_create_client(uid, full_name(message.from_user), username=message.from_user.username or "")
    db.log_message(client["id"], "in", message.caption or "", photo=message.photo[-1].file_id)
    who = client_signature(uid, full_name(message.from_user), message.from_user.username)
    admin_ids = {a["tg_id"] for a in db.admins()}
    sent = False
    for chat in targets:
        try:
            res = await message.bot.copy_message(
                chat, message.chat.id, message.message_id,
                caption=f"📸 Фото от клиента {who} ({about})" + (f"\n{message.caption}" if message.caption else ""),
                reply_markup=notify.reply_kb(client["id"]) if chat in admin_ids else None)
            if getattr(res, "message_id", None):
                db.add_relay(chat, res.message_id, client["id"])
            sent = True
        except Exception as e:
            log.warning("photo forward: %s", e)
    await message.answer("Фото передано ✅" if sent else "Не получилось передать фото, попробуйте позже.",
                         reply_markup=MENU)


@router.message(F.text)
async def client_text(message: Message):
    """Клиент написал что-то своё — не молчим: передаём администратору и показываем меню."""
    uid = message.from_user.id
    if is_staff(uid):
        await message.answer("Чтобы ответить клиентке — нажмите «💬 Ответить» под её сообщением "
                             "или ответьте на него свайпом. Записи и настройки — /start.")
        return
    client = db.get_or_create_client(uid, full_name(message.from_user), username=message.from_user.username or "")
    live = db.recently_answered(client["id"])
    db.log_message(client["id"], "in", message.text)
    who = client_signature(uid, full_name(message.from_user), message.from_user.username)
    for a in db.admins():
        try:
            m = await message.bot.send_message(a["tg_id"], f"💬 Сообщение от клиента {who}:\n\n{html.escape(message.text)}",
                                               reply_markup=notify.reply_kb(client["id"]))
            db.add_relay(a["tg_id"], m.message_id, client["id"])
        except Exception as e:
            log.warning("relay to admin: %s", e)
    if not live:  # во время живого разговора не повторяем автоответ
        await message.answer(
            "Спасибо! Я передал ваше сообщение администратору салона — вам ответят здесь же, в этом чате 💌\n\n"
            f"А записаться можно прямо сейчас — нажмите <b>{B_BOOK}</b> {below(message.chat.id)} 👇",
            reply_markup=MENU)
