"""Бот техподдержки (один на весь сервер).

Позволяет владельцу проекта (username = LOG_OWNER) войти в ЛЮБОЙ салон в роли
клиента, хозяина или мастера. Режим пишется в settings того салона (ключ dev_mode),
а личность разработчика — в shared/admin.json (dev_id). Салон показывает владельцу
в кабинете строку «Техподдержка подключена», так что доступ не скрытый.

Запускается в менеджере (manager.py). Своего токена нет — берётся ADMIN_BOT_TOKEN.
"""
import asyncio
import html
import json
import re
import logging
import sqlite3
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import config
from .config import LOG_OWNER, SHARED_DIR

log = logging.getLogger("adminbot")
router = Router()
# Менеджер подставляет сюда функции создания и запуска салона (бот работает внутри менеджера)
CREATE_SALON = None
START_SALON = None


DISABLE_SALON = ENABLE_SALON = REMOVE_SALON = None


class AddSalon(StatesGroup):
    name = State()
    niche = State()
    token = State()
    max_token = State()

BASE = Path(__file__).resolve().parent.parent
TENANTS = BASE / "tenants"
ROLES = {"client": "👤 Клиент", "owner": "👑 Хозяин", "master": "✂️ Мастер"}


def _allowed(message_or_cb) -> bool:
    """Только владелец платформы. Если его id уже сохранён — проверяем по id (username можно сменить или занять)."""
    u = message_or_cb.from_user
    did = _admin_data().get("dev_id")
    if did:
        return u.id == int(did)
    return (u.username or "").lower() == LOG_OWNER


def _admin_data() -> dict:
    try:
        return json.loads((SHARED_DIR / "admin.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_admin_data(**kv):
    d = _admin_data()
    d.update(kv)
    SHARED_DIR.mkdir(exist_ok=True)
    (SHARED_DIR / "admin.json").write_text(json.dumps(d), encoding="utf-8")


def set_dev_id(tg_id: int):
    _save_admin_data(dev_id=tg_id)  # остальное (PIN) сохраняется


def _tenant_cfg(slug: str) -> dict:
    data = {}
    f = TENANTS / slug / "tenant.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"')
    return data


def _salons() -> list[dict]:
    out = []
    if not TENANTS.exists():
        return out
    for d in sorted(TENANTS.iterdir()):
        if not (d.is_dir() and (d / "tenant.env").exists()):
            continue
        cfg = _tenant_cfg(d.name)
        bot = ""
        try:
            bot = json.loads((d / "status.json").read_text(encoding="utf-8")).get("bot") or ""
        except Exception:
            pass
        out.append({"slug": d.name, "name": cfg.get("NAME", d.name), "disabled": cfg.get("DISABLED") == "1",
                    "db": d / (cfg.get("DB_FILE") or "lunka.db"), "bot": bot})
    return out


def _salon(slug: str) -> dict | None:
    return next((s for s in _salons() if s["slug"] == slug), None)


def _db(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _set_mode(db_path: Path, mode: str):
    with _db(db_path) as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dev_mode', ?)", (mode,))
        c.commit()


def _masters(db_path: Path) -> list[dict]:
    try:
        with _db(db_path) as c:
            return [dict(r) for r in c.execute("SELECT id, name FROM masters WHERE active=1 ORDER BY id")]
    except Exception as e:
        log.warning("masters %s: %s", db_path, e)
        return []


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _show_salons(target):
    salons = _salons()
    if not salons:
        await target.answer("Пока нет ни одного салона на сервере.", reply_markup=_kb([[_btn("➕ Добавить салон", "dv:add")]]))
        return
    rows = [[_btn(("⏸ " if s["disabled"] else "") + s["name"], f"dv:s:{s['slug']}")] for s in salons]
    rows.append([_btn("➕ Добавить салон", "dv:add")])
    text = "🛠 <b>Техподдержка</b>\n\nВыберите салон, в который хотите войти, или добавьте новый:"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=_kb(rows))
    else:
        await target.message.edit_text(text, reply_markup=_kb(rows))


@router.message(CommandStart())
async def start(message: Message):
    if not _allowed(message):
        await message.answer("Это служебный бот проекта. Доступ закрыт.")
        return
    set_dev_id(message.from_user.id)
    await _show_salons(message)


@router.message(Command("stop"))
async def stop(message: Message):
    if not _allowed(message):
        return
    for s in _salons():
        try:
            _set_mode(s["db"], "")
        except Exception:
            pass
    await message.answer("Режим техподдержки выключен во всех салонах. Вы снова обычный пользователь.")


@router.callback_query(F.data == "dv:home")
async def back_home(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    await cb.answer()
    await _show_salons(cb)


@router.callback_query(F.data.startswith("dv:s:"))
async def pick_salon(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    slug = cb.data.split(":", 2)[2]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    if s["disabled"]:
        rows = [[_btn("▶️ Включить салон", f"dv:on:{slug}")], [_btn("🗑 Удалить салон", f"dv:del:{slug}")],
                [_btn("◀️ Назад", "dv:home")]]
        await cb.message.edit_text(f"⏸ Салон «{s['name']}» отключён: бот не отвечает клиентам, данные сохранены.",
                                   reply_markup=_kb(rows))
        return
    rows = [[_btn(ROLES["client"], f"dv:r:{slug}:client")],
            [_btn(ROLES["owner"], f"dv:r:{slug}:owner")],
            [_btn(ROLES["master"], f"dv:m:{slug}")],
            [_btn("👑 Владелец салона", f"dv:own:{slug}")],
            [_btn("⏸ Отключить", f"dv:off:{slug}"), _btn("🗑 Удалить", f"dv:del:{slug}")],
            [_btn("◀️ Назад", "dv:home")]]
    await cb.message.edit_text(f"Салон «{s['name']}». В какой роли войти?", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("dv:m:"))
async def pick_master_list(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    slug = cb.data.split(":", 2)[2]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    ms = _masters(s["db"])
    if not ms:
        await cb.message.edit_text("В этом салоне пока нет мастеров.",
                                   reply_markup=_kb([[_btn("◀️ Назад", f"dv:s:{slug}")]]))
        return
    rows = [[_btn(m["name"], f"dv:r:{slug}:master:{m['id']}")] for m in ms]
    rows.append([_btn("◀️ Назад", f"dv:s:{slug}")])
    await cb.message.edit_text(f"Салон «{s['name']}». Войти как мастер:", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("dv:r:"))
async def apply_role(cb: CallbackQuery):
    if not _allowed(cb):
        await cb.answer()
        return
    parts = cb.data.split(":")  # dv:r:<slug>:<role>[:<mid>]
    slug, role = parts[2], parts[3]
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    mode = f"master:{parts[4]}" if role == "master" else role
    try:
        _set_mode(s["db"], mode)
    except Exception as e:
        await cb.answer(f"Не удалось: {e}", show_alert=True)
        return
    set_dev_id(cb.from_user.id)
    await cb.answer("Готово")
    label = ROLES.get(role, role)
    if s["bot"]:
        link = f"https://t.me/{s['bot']}?start=dev"
        open_row = [[InlineKeyboardButton(text=f"Открыть «{s['name']}»", url=link)]]
    else:
        open_row = []
    await cb.message.edit_text(
        f"✅ В салоне «{s['name']}» вы теперь входите как <b>{label}</b>.\n\n"
        f"Откройте бота салона и нажмите «Запустить» (/start) — увидите его меню в этой роли. "
        f"Чтобы вернуться в обычный режим — /stop.",
        reply_markup=_kb(open_row + [[_btn("◀️ К списку салонов", "dv:home")]]))


async def run():
    token = config.ADMIN_BOT_TOKEN
    if not token:
        return
    try:
        bot = Bot(token, session=config.tg_session(), default=DefaultBotProperties(parse_mode="HTML"))
        me = await bot.get_me()
        log.info("Бот техподдержки: https://t.me/%s (доступ: @%s)", me.username, LOG_OWNER)
        dp = Dispatcher()
        dp.include_router(router)
        await dp.start_polling(bot, handle_signals=False, polling_timeout=config.POLL_TIMEOUT)
    except Exception as e:
        log.warning("Бот техподдержки не запустился (проверьте ADMIN_BOT_TOKEN): %s", e)



# ======================================================================
#  ➕ Новый салон прямо из бота: название → тип → токен → MAX → запущен
# ======================================================================
NICHE_BTNS = [("💅 Маникюр / ногти", "nails"), ("✂️ Парикмахерская", "hair")]


@router.message(Command("add"))
@router.callback_query(F.data == "dv:add")
async def add_start(event, state: FSMContext):
    if not _allowed(event):
        return
    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    if not CREATE_SALON:
        await msg.answer("Добавлять салоны можно, когда бот работает внутри сервиса (manager.py).")
        return
    await state.clear()
    await state.set_state(AddSalon.name)
    await msg.answer("➕ <b>Новый салон — шаг 1 из 4</b>\n\nКак называется салон или мастер? Так его увидят клиенты.\n"
                     "<i>Например: Ногти у Анны, Barber Room, Студия «Лиса»</i>",
                     reply_markup=_kb([[_btn("Отмена", "dv:add_cancel")]]))


@router.callback_query(F.data == "dv:add_cancel")
async def add_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.edit_text("Добавление салона отменено.")


@router.message(AddSalon.name, F.text)
async def add_name(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    name = message.text.strip()[:60]
    if len(name) < 2 or name.startswith("/"):
        await message.answer("Напишите название салона — хотя бы 2 буквы.")
        return
    await state.update_data(name=name)
    await message.answer(f"«{name}» 👍\n\n<b>Шаг 2 из 4.</b> Чем занимается салон? От этого зависят тексты в боте и стартовый прайс.",
                         reply_markup=_kb([[_btn(t, f"dv:niche:{k}")] for t, k in NICHE_BTNS]
                                          + [[_btn("✍️ Свой вариант — напишу сам", "dv:niche_own")], [_btn("Отмена", "dv:add_cancel")]]))


@router.callback_query(F.data == "dv:niche_own")
async def add_niche_own(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb) or not (await state.get_data()).get("name"):
        await cb.answer()
        return
    await cb.answer()
    await state.set_state(AddSalon.niche)
    await cb.message.edit_text("Напишите, чем занимается салон — одним-двумя словами.\n"
                               "<i>Например: брови и ресницы, массаж, барбершоп, тату, косметология</i>",
                               reply_markup=_kb([[_btn("Отмена", "dv:add_cancel")]]))


@router.message(AddSalon.niche, F.text)
async def add_niche_text(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    title = message.text.strip()[:40]
    if len(title) < 3 or title.startswith("/"):
        await message.answer("Напишите тип салона — хотя бы 3 буквы, например «массаж».")
        return
    from .niche import emoji_for
    await state.update_data(niche="custom", niche_title=title)
    await state.set_state(AddSalon.token)
    await message.answer(f"Тип: {emoji_for(title)} {title}. Услуги и цены владелец внесёт в кабинете сам.\n\n" + TOKEN_STEP,
                         reply_markup=_kb([[_btn("Отмена", "dv:add_cancel")]]))


TOKEN_STEP = ("<b>Шаг 3 из 4. Бот в Telegram</b>\n\n"
              "1. Откройте @BotFather → /newbot\n"
              "2. Имя — название салона, адрес — любой свободный, на конце _bot\n"
              "3. Скопируйте токен (выглядит так: <code>1234567890:AAH…</code>) и пришлите сюда.\n\n"
              "🔒 Сообщение с токеном я сразу удалю из чата.")


@router.callback_query(F.data.startswith("dv:niche:"))
async def add_niche(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb) or not (await state.get_data()).get("name"):
        await cb.answer()
        return
    await cb.answer()
    await state.update_data(niche=cb.data.split(":")[2])
    await state.set_state(AddSalon.token)
    await cb.message.edit_text(
        "<b>Шаг 3 из 4. Бот в Telegram</b>\n\n"
        "1. Откройте @BotFather → /newbot\n"
        "2. Имя — название салона, адрес — любой свободный, на конце _bot\n"
        "3. Скопируйте токен (выглядит так: <code>1234567890:AAH…</code>) и пришлите сюда.\n\n"
        "🔒 Сообщение с токеном я сразу удалю из чата.",
        reply_markup=_kb([[_btn("Отмена", "dv:add_cancel")]]))


async def _check_tg_token(token: str) -> str:
    """Проверить токен в Telegram (через мост). Возвращает @имя бота или бросает ошибку."""
    test = Bot(token, session=config.tg_session())
    try:
        me = await test.get_me()
        return me.username
    finally:
        await test.session.close()


@router.message(AddSalon.token, F.text)
async def add_token(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    token = message.text.strip().strip('"')
    try:
        await message.delete()  # токен — секрет, в истории чата ему не место
    except Exception:
        pass
    if ":" not in token or len(token) < 30:
        await message.answer("Это не похоже на токен. Он выглядит так: <code>1234567890:AAH…</code> — пришлите ещё раз.")
        return
    try:
        username = await _check_tg_token(token)
    except Exception:
        await message.answer("❌ Telegram не принял этот токен. Скопируйте его из @BotFather ещё раз целиком и пришлите сюда.")
        return
    await state.update_data(token=token, username=username)
    await state.set_state(AddSalon.max_token)
    await message.answer(f"✅ Бот @{username} подходит.\n\n<b>Шаг 4 из 4. Бот в MAX</b> (по желанию)\n"
                         "Если у салона есть бот в MAX — пришлите его токен (business.max.ru → Чат-боты → "
                         "Расширенные настройки). Если нет — нажмите «Пропустить», подключить можно позже.",
                         reply_markup=_kb([[_btn("Пропустить →", "dv:max_skip")], [_btn("Отмена", "dv:add_cancel")]]))


async def _check_max_token(token: str) -> str:
    import httpx
    from .maxbot import API, ssl_context
    async with httpx.AsyncClient(timeout=15, verify=ssl_context()) as c:
        r = await c.get(f"{API}/me", headers={"Authorization": token})
        r.raise_for_status()
        return r.json().get("username") or ""


@router.message(AddSalon.max_token, F.text)
async def add_max(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    token = message.text.strip().strip('"')
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await _check_max_token(token)
    except Exception:
        await message.answer("❌ MAX не принял этот токен. Пришлите ещё раз или нажмите «Пропустить».",
                             reply_markup=_kb([[_btn("Пропустить →", "dv:max_skip")]]))
        return
    await finish_add(message, state, token)


@router.callback_query(F.data == "dv:max_skip")
async def add_max_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if _allowed(cb) and (await state.get_data()).get("token"):
        await finish_add(cb.message, state, "")


async def finish_add(message: Message, state: FSMContext, max_token: str):
    d = await state.get_data()
    await state.clear()
    try:
        slug = CREATE_SALON(d["name"], d["niche"], d["token"], max_token, d.get("niche_title", ""))
        cabinet = await START_SALON(slug)
    except Exception as e:
        await message.answer(f"❌ Не получилось добавить салон: {e}")
        return
    link = f"https://t.me/{d['username']}"
    _BOTS[slug] = d["username"]
    await message.answer(
        f"🎉 <b>Салон «{d['name']}» запущен!</b>\n\n"
        f"Бот: {link}\n" + (f"Кабинет: {cabinet}\n" if cabinet else "") +
        f"Папка на сервере: <code>{slug}</code>\n\n"
        "<b>Последний шаг — владелец.</b> Как работает заказчик?",
        reply_markup=_owner_kb(slug, solo_ok=True, back=False),
        disable_web_page_preview=True)


# ======================================================================
#  👑 Назначить владельца: формат (частник / салон) → @username заказчика
#  Telegram не отдаёт id по @username, пока человек сам не написал боту,
#  поэтому сохраняем «ожидание»: кто первым придёт с этим @username
#  (или по личной ссылке owner_<код>) — тот и владелец, в выбранном формате.
# ======================================================================
_BOTS: dict[str, str] = {}  # slug → @бот (для только что созданных, пока нет status.json)


class OwnerSet(StatesGroup):
    username = State()


def _owner_kb(slug: str, solo_ok: bool, back: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if solo_ok:
        rows.append([_btn("💅 Частный мастер (работает на себя)", f"dv:ow:{slug}:solo")])
    rows.append([_btn("🏠 Салон с мастерами", f"dv:ow:{slug}:salon")])
    if back:
        rows.append([_btn("◀️ Назад", f"dv:s:{slug}")])
    return _kb(rows)


def _owner_info(db_path: Path) -> tuple[list[str], dict, int]:
    """(имена владельцев, настройки ожидания, сколько мастеров)"""
    try:
        with _db(db_path) as c:
            names = [r["name"] for r in c.execute("SELECT name FROM admins")]
            st = {r["key"]: r["value"] for r in c.execute(
                "SELECT key, value FROM settings WHERE key IN ('owner_username','owner_code','owner_mode','mode')")}
            n = c.execute("SELECT COUNT(*) FROM masters WHERE active=1").fetchone()[0]
        return names, st, n
    except sqlite3.Error:
        return [], {}, 0  # салон только что создан — база ещё пустая


def _save_owner(db_path: Path, mode: str, username: str, code: str):
    with _db(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for k, v in (("owner_mode", mode), ("owner_username", username), ("owner_code", code)):
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        c.commit()


def _bot_of(s: dict) -> str:
    return s.get("bot") or _BOTS.get(s["slug"], "")


MODE_TITLE = {"solo": "💅 частный мастер", "salon": "🏠 салон с мастерами"}


@router.callback_query(F.data.startswith("dv:own:"))
async def owner_menu(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb):
        await cb.answer()
        return
    s = _salon(cb.data.split(":", 2)[2])
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    await state.clear()
    names, st, n_masters = _owner_info(s["db"])
    text = f"👑 <b>Владелец салона «{s['name']}»</b>\n\n"
    text += ("Сейчас: " + ", ".join(names) + "\n") if names else "Владельца пока нет.\n"
    if st.get("owner_code"):
        who = f"@{st['owner_username']}" if st.get("owner_username") else "по личной ссылке"
        text += f"⏳ Ждём: {who}, формат — {MODE_TITLE.get(st.get('owner_mode'), '?')}\n"
        bot = _bot_of(s)
        if bot:
            text += f"Ссылка владельца: https://t.me/{bot}?start=owner_{st['owner_code']}\n"
    text += "\nНазначить (нового) владельца — выберите формат работы заказчика:"
    solo_ok = n_masters <= 1
    if not solo_ok:
        text += "\n<i>«Частный мастер» недоступен: в салоне уже несколько мастеров.</i>"
    kb = _owner_kb(s["slug"], solo_ok)
    if st.get("owner_code"):
        kb.inline_keyboard.insert(-1, [_btn("✖️ Отменить ожидание", f"dv:ow_clear:{s['slug']}")])
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("dv:ow_clear:"))
async def owner_clear(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb):
        await cb.answer()
        return
    s = _salon(cb.data.split(":", 2)[2])
    if s:
        _save_owner(s["db"], "", "", "")
    await cb.answer("Ожидание отменено, ссылка больше не работает", show_alert=True)
    await state.clear()
    await cb.message.edit_text("✖️ Назначение владельца отменено.",
                               reply_markup=_kb([[_btn("◀️ К салону", f"dv:s:{cb.data.split(':', 2)[2]}")]]))


@router.callback_query(F.data.regexp(r"^dv:ow:[^:]+:(solo|salon)$"))
async def owner_mode(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb):
        await cb.answer()
        return
    _, _, slug, mode = cb.data.split(":")
    if not _salon(slug):
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    await state.set_state(OwnerSet.username)
    await state.update_data(owner_slug=slug, owner_mode=mode)
    await cb.message.edit_text(
        f"Формат: <b>{MODE_TITLE[mode]}</b>\n\n"
        "Пришлите <b>@username</b> заказчика в Telegram (например @maria_nails).\n"
        "Когда он откроет бота салона и нажмёт «Запустить» — сразу станет владельцем.\n\n"
        "Нет username? Нажмите «Только ссылка» — пришлю личную ссылку владельца.",
        reply_markup=_kb([[_btn("🔗 Только ссылка", "dv:ow_link")], [_btn("◀️ Назад", f"dv:own:{slug}")]]))


async def _finish_owner(message: Message, state: FSMContext, username: str):
    d = await state.get_data()
    await state.clear()
    s = _salon(d.get("owner_slug", ""))
    if not s:
        await message.answer("Салон не найден.")
        return
    code = _secrets.token_hex(5)
    try:
        _save_owner(s["db"], d["owner_mode"], username, code)
    except sqlite3.Error as e:
        await message.answer(f"❌ Не удалось сохранить: {e}")
        return
    bot = _bot_of(s)
    link = f"https://t.me/{bot}?start=owner_{code}" if bot else f"(бот салона)?start=owner_{code}"
    who = (f"Как только <b>@{username}</b> откроет бота и нажмёт «Запустить», он станет владельцем.\n"
           "Или отправьте ему личную ссылку — сработает то, что будет первым:\n") if username else \
          "Отправьте заказчику эту личную ссылку — кто откроет её первым, станет владельцем:\n"
    await message.answer(
        f"✅ Салон «{s['name']}», формат — <b>{MODE_TITLE[d['owner_mode']]}</b>.\n\n" + who + link +
        "\n\nСсылка одноразовая. Сами по ней не переходите — вы входите через «В какой роли войти».",
        reply_markup=_kb([[_btn("◀️ К салону", f"dv:s:{s['slug']}")]]),
        disable_web_page_preview=True)


@router.callback_query(F.data == "dv:ow_link")
async def owner_link_only(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if _allowed(cb) and (await state.get_data()).get("owner_slug"):
        await _finish_owner(cb.message, state, "")


@router.message(OwnerSet.username)
async def owner_username(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    raw = (message.text or "").strip()
    m = re.fullmatch(r"(?:https?://)?(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})", raw)
    if not m:
        await message.answer("Это не похоже на username. Пришлите в виде @maria_nails или нажмите «Только ссылка».",
                             reply_markup=_kb([[_btn("🔗 Только ссылка", "dv:ow_link")]]))
        return
    await _finish_owner(message, state, m.group(1).lower())



# ======================================================================
#  ⏸ Отключить / ▶️ Включить / 🗑 Удалить — только с PIN-кодом
# ======================================================================
import hashlib  # noqa: E402
import secrets as _secrets  # noqa: E402
import time as _time  # noqa: E402

PIN_TRIES, PIN_LOCK = 3, 15 * 60
_fails = {"n": 0, "until": 0.0}


class Danger(StatesGroup):
    new_pin = State()
    pin = State()
    confirm_name = State()


def _pin_hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def pin_is_set() -> bool:
    return bool(_admin_data().get("pin_hash"))


def check_pin(pin: str) -> bool:
    d = _admin_data()
    return bool(d.get("pin_hash")) and _secrets.compare_digest(_pin_hash(pin, d.get("pin_salt", "")), d["pin_hash"])


ACTIONS = {"off": "⏸ отключить", "on": "▶️ включить", "del": "🗑 удалить"}


@router.callback_query(F.data.regexp(r"^dv:(off|on|del):"))
async def danger_start(cb: CallbackQuery, state: FSMContext):
    if not _allowed(cb):
        await cb.answer()
        return
    _, action, slug = cb.data.split(":", 2)
    s = _salon(slug)
    if not s:
        await cb.answer("Салон не найден", show_alert=True)
        return
    await cb.answer()
    if _time.time() < _fails["until"]:
        mins = int((_fails["until"] - _time.time()) // 60) + 1
        await cb.message.answer(f"🔒 Слишком много неверных PIN-кодов. Попробуйте через {mins} мин.")
        return
    await state.clear()
    await state.update_data(action=action, slug=slug)
    if not pin_is_set():
        await state.set_state(Danger.new_pin)
        await cb.message.answer("🔐 <b>Сначала придумайте PIN-код</b> — 4–8 цифр. Он будет нужен, чтобы отключать "
                                "и удалять салоны. Сообщение с ним я сразу удалю.\n\nЗапомните его — восстановить нельзя "
                                "(только удалив строки pin_* из shared/admin.json на сервере).",
                                reply_markup=_kb([[_btn("Отмена", "dv:danger_cancel")]]))
        return
    await state.set_state(Danger.pin)
    await cb.message.answer(f"Вы хотите {ACTIONS[action]} салон «{html.escape(s['name'])}».\n\n🔐 Введите PIN-код:",
                            reply_markup=_kb([[_btn("Отмена", "dv:danger_cancel")]]))


@router.callback_query(F.data == "dv:danger_cancel")
async def danger_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.edit_text("Отменено, ничего не изменилось.")


async def _read_secret(message: Message) -> str:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    return text


@router.message(Danger.new_pin, F.text)
async def danger_new_pin(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    pin = await _read_secret(message)
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        await message.answer("PIN — от 4 до 8 цифр. Придумайте ещё раз:")
        return
    salt = _secrets.token_hex(16)
    _save_admin_data(pin_salt=salt, pin_hash=_pin_hash(pin, salt))
    await state.set_state(Danger.pin)
    await message.answer("✅ PIN-код сохранён. Теперь введите его ещё раз, чтобы подтвердить действие:")


@router.message(Danger.pin, F.text)
async def danger_pin(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    pin = await _read_secret(message)
    if not check_pin(pin):
        _fails["n"] += 1
        if _fails["n"] >= PIN_TRIES:
            _fails.update(n=0, until=_time.time() + PIN_LOCK)
            await state.clear()
            log.warning("Бот техподдержки: 3 неверных PIN — блокировка на 15 минут")
            await message.answer("🔒 Неверный PIN 3 раза подряд. Опасные действия заблокированы на 15 минут.")
            return
        await message.answer(f"❌ Неверный PIN. Осталось попыток: {PIN_TRIES - _fails['n']}")
        return
    _fails["n"] = 0
    d = await state.get_data()
    s = _salon(d.get("slug", ""))
    if not s:
        await state.clear()
        await message.answer("Салон не найден.")
        return
    if d["action"] == "del":
        await state.set_state(Danger.confirm_name)
        await message.answer(f"⚠️ Удаление: бот «{html.escape(s['name'])}» перестанет работать, салон пропадёт из списка. "
                             "Данные (клиенты, записи, отзывы) переедут в архив на сервере — их можно будет вернуть.\n\n"
                             f"Чтобы подтвердить, напишите название салона точно:\n<code>{html.escape(s['name'])}</code>",
                             reply_markup=_kb([[_btn("Отмена", "dv:danger_cancel")]]))
        return
    await state.clear()
    if d["action"] == "off":
        DISABLE_SALON(s["slug"])
        await message.answer(f"⏸ Салон «{html.escape(s['name'])}» отключён. Бот не отвечает клиентам, все данные сохранены. "
                             "Включить снова — в списке салонов.", reply_markup=_kb([[_btn("◀️ К салонам", "dv:home")]]))
    else:
        await ENABLE_SALON(s["slug"])
        await message.answer(f"▶️ Салон «{html.escape(s['name'])}» снова работает.", reply_markup=_kb([[_btn("◀️ К салонам", "dv:home")]]))


@router.message(Danger.confirm_name, F.text)
async def danger_confirm(message: Message, state: FSMContext):
    if not _allowed(message):
        return
    d = await state.get_data()
    s = _salon(d.get("slug", ""))
    await state.clear()
    if not s or message.text.strip() != s["name"]:
        await message.answer("Название не совпало — удаление отменено, ничего не изменилось.")
        return
    archived = REMOVE_SALON(s["slug"])
    await message.answer(f"🗑 Салон «{html.escape(s['name'])}» удалён.\nДанные в архиве на сервере: <code>archive/{archived}</code>\n"
                         "Вернуть: переложить эту папку в tenants/ под старым именем и перезапустить сервис.",
                         reply_markup=_kb([[_btn("◀️ К салонам", "dv:home")]]))
