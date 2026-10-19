"""Бот в MAX — тот же самый сценарий, что и в Telegram.

Вместо второго бота «с нуля» здесь переводчик: обновления MAX превращаются в обычные
сообщения/нажатия aiogram и идут в ТЕ ЖЕ обработчики, что и Telegram (app/bot.py),
а ответы бота (send_message, edit_text, кнопки…) переводятся обратно в API MAX.
Поэтому любые правки сценария автоматически работают в обоих мессенджерах.

Пользователи MAX хранятся в базе с ОТРИЦАТЕЛЬНЫМ id (−id), Telegram — с положительным.
Так салон видит один общий список клиентов, а уведомления сами уходят в нужный мессенджер:
мастер в Telegram получит запись клиентки из MAX и наоборот.

Документация API: https://dev.max.ru/docs-api (адрес platform-api2.max.ru, токен в заголовке Authorization).
"""
import asyncio
import io
import logging
import re
import time
from datetime import datetime

import certifi
import httpx
import ssl
from pathlib import Path
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.types import (CallbackQuery, Chat, Contact, InlineKeyboardMarkup, Message, MessageId, PhotoSize,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, User)

from . import db
from .config import runtime

log = logging.getLogger("max")
API = "https://platform-api2.max.ru"
CERTS = Path(__file__).resolve().parent / "certs" / "russian_trusted_ca.pem"


def ssl_context() -> ssl.SSLContext:
    """Сертификат серверов MAX выдан удостоверяющим центром Минцифры (Russian Trusted Root CA),
    которого нет в стандартном списке Python. Доверяем ему дополнительно — только для запросов к MAX.
    Файл взят с официального сайта Госуслуг (gu-st.ru), действует до 2032 года."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if CERTS.exists():
        ctx.load_verify_locations(str(CERTS))
    return ctx
UPDATE_TYPES = "message_created,message_callback,bot_started"


def to_db_id(max_user_id: int) -> int:
    return -int(max_user_id)


def from_db_id(db_id: int) -> int:
    return -int(db_id)


class IdMap:
    """MAX называет сообщения строками (mid), aiogram ждёт числа — храним соответствие."""

    def __init__(self):
        self._next = 1_000_000
        self.by_mid: dict[str, int] = {}
        self.by_int: dict[int, dict] = {}

    def put(self, mid: str, **info) -> int:
        if mid in self.by_mid:
            i = self.by_mid[mid]
            self.by_int[i].update({k: v for k, v in info.items() if v is not None})
            return i
        self._next += 1
        self.by_mid[mid] = self._next
        self.by_int[self._next] = {"mid": mid, **info}
        return self._next

    def get(self, i: int) -> dict | None:
        return self.by_int.get(i)


def phone_from_vcf(vcf: str | None) -> str:
    m = re.search(r"TEL[^:]*:([+\d\s\-()]+)", vcf or "")
    if not m:
        return ""
    digits = re.sub(r"[^\d+]", "", m.group(1))
    return digits if digits.startswith("+") else "+" + digits


# ======================================================================
#  Отправка: вызовы aiogram → API MAX
# ======================================================================
class MaxSession(BaseSession):
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        super().__init__()
        self.http = http or httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(45, connect=15),
                                              headers={"Authorization": token}, verify=ssl_context())
        self.ids = IdMap()
        self.tg_bot: Bot | None = None   # сообщения в Telegram (id > 0) уходят через него
        self.me: dict = {}

    async def close(self):
        await self.http.aclose()

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        async with httpx.AsyncClient(timeout=timeout, verify=ssl_context()) as c:
            async with c.stream("GET", url, headers=headers) as r:
                async for chunk in r.aiter_bytes(chunk_size):
                    yield chunk

    # --- служебное ---
    async def call(self, method: str, path: str, params=None, json=None):
        for attempt in range(3):
            r = await self.http.request(method, path, params=params, json=json)
            if r.status_code == 429:  # слишком часто — подождать и повторить
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"MAX API {method} {path}: {r.status_code} {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"MAX API {method} {path}: 429 Too Many Requests")

    def attachments(self, markup, chat_id: int) -> list:
        """Кнопки aiogram → клавиатура MAX. Нижнего меню в MAX нет — его кнопки ставим под сообщением."""
        if markup is None or isinstance(markup, ReplyKeyboardRemove):
            return []
        rows = []
        if isinstance(markup, InlineKeyboardMarkup):
            for row in markup.inline_keyboard:
                out = []
                for b in row:
                    if b.callback_data:
                        out.append({"type": "callback", "text": b.text, "payload": b.callback_data})
                    elif b.url:
                        out.append({"type": "link", "text": b.text, "url": b.url})
                    elif b.web_app:  # кабинет: в MAX открываем по личной ссылке входа
                        out.append({"type": "link", "text": b.text, "url": cabinet_link(b.web_app.url, chat_id)})
                if out:
                    rows.append(out)
        elif isinstance(markup, ReplyKeyboardMarkup):
            for row in markup.keyboard:
                rows.append([{"type": "request_contact", "text": b.text} if b.request_contact
                             else {"type": "message", "text": b.text} for b in row])
        return [{"type": "inline_keyboard", "payload": {"buttons": rows}}] if rows else []

    def fake_message(self, mid: str, chat_id: int, text: str | None, atts: list) -> Message:
        i = self.ids.put(mid, text=text, atts=atts, chat_id=chat_id)
        return Message(message_id=i, date=datetime.now(), chat=Chat(id=chat_id, type="private"), text=text)

    # --- главный перехватчик ---
    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        cid = getattr(method, "chat_id", None)
        if isinstance(cid, int) and cid > 0 and self.tg_bot is not None:
            return await self.tg_bot(method)  # адресат в Telegram
        handler = getattr(self, "m_" + name, None)
        if handler is None:
            log.debug("MAX: метод %s пропущен", name)
            return True
        return await handler(method)

    async def m_GetMe(self, m):
        me = self.me or await self.call("GET", "/me")
        self.me = me
        return User(id=me.get("user_id", 1), is_bot=True, first_name=me.get("first_name") or me.get("name") or "bot",
                    username=me.get("username"))

    async def m_SendMessage(self, m):
        atts = self.attachments(m.reply_markup, m.chat_id)
        body = {"text": m.text, "format": "html", "attachments": atts}
        res = await self.call("POST", "/messages", params={"user_id": from_db_id(m.chat_id),
                                                           "disable_link_preview": "true"}, json=body)
        mid = (res.get("message") or {}).get("body", {}).get("mid") or f"local-{time.time()}"
        return self.fake_message(mid, m.chat_id, m.text, atts)

    async def _edit(self, message_id: int, text: str | None, atts: list, chat_id: int):
        info = self.ids.get(message_id)
        if not info or info["mid"].startswith("local-"):
            raise RuntimeError("MAX: сообщение для изменения не найдено")  # safe_edit отправит новое
        await self.call("PUT", "/messages", params={"message_id": info["mid"]},
                        json={"text": text, "format": "html", "attachments": atts})
        self.ids.put(info["mid"], text=text, atts=atts)
        return Message(message_id=message_id, date=datetime.now(), chat=Chat(id=chat_id, type="private"), text=text)

    async def m_EditMessageText(self, m):
        return await self._edit(m.message_id, m.text, self.attachments(m.reply_markup, m.chat_id), m.chat_id)

    async def m_EditMessageReplyMarkup(self, m):
        info = self.ids.get(m.message_id) or {}
        return await self._edit(m.message_id, info.get("text"), self.attachments(m.reply_markup, m.chat_id), m.chat_id)

    async def m_AnswerCallbackQuery(self, m):
        if m.text:
            await self.call("POST", "/answers", params={"callback_id": m.callback_query_id},
                            json={"notification": m.text})
        return True

    async def m_DeleteMessage(self, m):
        info = self.ids.get(m.message_id)
        if info:
            await self.call("DELETE", "/messages", params={"message_id": info["mid"]})
        return True

    async def m_SetMyCommands(self, m):
        cmds = [{"name": c.command, "description": c.description} for c in m.commands]
        await self.call("PATCH", "/me/commands", json={"commands": cmds})
        return True

    async def m_SetChatMenuButton(self, m):
        return True  # в MAX нет кнопки меню у поля ввода — кабинет открывается кнопкой-ссылкой

    async def m_CopyMessage(self, m):
        """Переслать фото. Источник — сообщение из MAX (from_chat_id < 0) или из Telegram (> 0)."""
        caption = m.caption or ""
        if m.from_chat_id < 0:
            src = self.ids.get(m.message_id) or {}
            photo = src.get("photo")
            atts = ([{"type": "image", "payload": {"token": photo["token"]}}] if photo else []) \
                + self.attachments(m.reply_markup, m.chat_id)
        else:  # фото из Telegram → скачать и загрузить в MAX
            atts = []
            try:
                atts = [await self.upload_from_telegram(m.from_chat_id, m.message_id)]
            except Exception as e:
                log.warning("Не удалось перенести фото из Telegram в MAX: %s", e)
                caption = (caption + "\n📷 (фото не удалось передать)").strip()
            atts += self.attachments(m.reply_markup, m.chat_id)
        res = await self.call("POST", "/messages", params={"user_id": from_db_id(m.chat_id)},
                              json={"text": caption or None, "format": "html", "attachments": atts})
        mid = (res.get("message") or {}).get("body", {}).get("mid") or f"local-{time.time()}"
        return MessageId(message_id=self.ids.put(mid, text=caption, atts=atts, chat_id=m.chat_id))

    async def upload_from_telegram(self, tg_chat: int, tg_message_id: int) -> dict:
        file_id = TG_PHOTOS.get((tg_chat, tg_message_id))
        if not file_id or not self.tg_bot:
            raise RuntimeError("нет фото")
        buf = io.BytesIO()
        await self.tg_bot.download(file_id, destination=buf)
        up = await self.call("POST", "/uploads", params={"type": "image"})
        async with httpx.AsyncClient(timeout=60, verify=ssl_context()) as c:
            r = await c.post(up["url"], files={"data": ("photo.jpg", buf.getvalue(), "image/jpeg")})
            r.raise_for_status()
            return {"type": "image", "payload": r.json()}


# file_id фото из Telegram, чтобы перекинуть их в MAX: (chat_id, message_id) → file_id
TG_PHOTOS: dict[tuple[int, int], str] = {}


class RoutingSession(AiohttpSession):
    """Сессия Telegram-бота: всё, что адресовано клиентам MAX (id < 0), уходит в MAX."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.max_session: MaxSession | None = None
        self.max_bot: Bot | None = None

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        cid = getattr(method, "chat_id", None)
        if self.max_session and isinstance(cid, int) and cid < 0:
            return await self.max_session.make_request(self.max_bot, method)
        if name == "CopyMessage" and self.max_session and method.from_chat_id < 0:
            return await self._photo_from_max(bot, method)  # фото клиентки из MAX → сотруднику в Telegram
        return await self._tg(bot, method, timeout)

    async def _tg(self, bot, method, timeout=None):
        """Настоящий запрос в Telegram."""
        return await super().make_request(bot, method, timeout)

    async def _photo_from_max(self, bot, m):
        from aiogram.methods import SendMessage, SendPhoto
        src = self.max_session.ids.get(m.message_id) or {}
        photo = src.get("photo")
        if photo and photo.get("url"):
            res = await self._tg(bot, SendPhoto(chat_id=m.chat_id, photo=photo["url"], caption=m.caption,
                                                reply_markup=m.reply_markup))
        else:
            res = await self._tg(bot, SendMessage(chat_id=m.chat_id, text=(m.caption or "") + "\n📷 фото",
                                                  reply_markup=m.reply_markup))
        return MessageId(message_id=res.message_id)


def cabinet_link(url: str, chat_id: int) -> str:
    """Личная ссылка входа в кабинет для сотрудника из MAX (открывается в браузере)."""
    token = db.issue_login(chat_id)
    return f"{url}{'&' if '?' in url else '?'}k={token}"


# ======================================================================
#  Приём: обновления MAX → объекты aiogram
# ======================================================================
def _user(u: dict) -> User:
    return User(id=to_db_id(u["user_id"]), is_bot=bool(u.get("is_bot")), first_name=u.get("first_name") or u.get("name") or "Клиент",
                last_name=u.get("last_name"), username=u.get("username"))


def convert(update: dict, session: MaxSession) -> Update | None:
    kind = update.get("update_type")
    if kind == "bot_started":
        user = _user(update["user"])
        payload = update.get("payload")
        text = "/start" + (f" {payload}" if payload else "")
        mid = session.ids.put(f"start-{update.get('timestamp')}-{user.id}", text=text)
        return Update(update_id=0, message=Message(message_id=mid, date=datetime.now(),
                                                    chat=Chat(id=user.id, type="private"), from_user=user, text=text))
    if kind == "message_created":
        msg = update["message"]
        sender = msg.get("sender") or {}
        if sender.get("is_bot") or (msg.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
            return None
        user = _user(sender)
        body = msg.get("body") or {}
        text = body.get("text") or None
        contact = photo = None
        for a in body.get("attachments") or []:
            p = a.get("payload") or {}
            if a.get("type") == "contact":
                info = p.get("max_info") or {}
                phone = phone_from_vcf(p.get("vcf_info"))
                if phone:
                    contact = Contact(phone_number=phone, first_name=info.get("first_name") or user.first_name,
                                      user_id=user.id)
            elif a.get("type") == "image" and p.get("token"):
                photo = {"token": p["token"], "url": p.get("url")}
        mid = session.ids.put(body.get("mid") or f"in-{time.time()}", text=text, photo=photo, chat_id=user.id)
        reply_to = None
        link = msg.get("link") or {}
        if link.get("type") == "reply":
            lmid = (link.get("message") or {}).get("mid")
            if lmid and lmid in session.ids.by_mid:
                reply_to = Message(message_id=session.ids.by_mid[lmid], date=datetime.now(),
                                   chat=Chat(id=user.id, type="private"), text="")
        photos = [PhotoSize(file_id=photo["token"], file_unique_id=photo["token"][:32], width=1, height=1)] if photo else None
        if not (text or contact or photos):
            return None
        return Update(update_id=0, message=Message(
            message_id=mid, date=datetime.now(), chat=Chat(id=user.id, type="private"), from_user=user,
            text=None if photos else text, caption=text if photos else None, contact=contact, photo=photos,
            reply_to_message=reply_to))
    if kind == "message_callback":
        cb = update["callback"]
        user = _user(cb["user"])
        body = (update.get("message") or {}).get("body") or {}
        mid = session.ids.put(body.get("mid") or f"cb-{time.time()}", text=body.get("text"), chat_id=user.id)
        return Update(update_id=0, callback_query=CallbackQuery(
            id=cb["callback_id"], from_user=user, chat_instance="max", data=cb.get("payload") or "",
            message=Message(message_id=mid, date=datetime.now(), chat=Chat(id=user.id, type="private"),
                            text=body.get("text"))))
    return None


async def poll(dp, max_bot: Bot, session: MaxSession):
    """Получать обновления MAX (long polling) и отдавать их общему сценарию бота."""
    marker = None
    pause = 1
    while True:
        try:
            params = {"timeout": 30, "types": UPDATE_TYPES}
            if marker is not None:
                params["marker"] = marker
            data = await session.call("GET", "/updates", params=params)
            pause = 1
            marker = data.get("marker", marker)
            for u in data.get("updates") or []:
                upd = convert(u, session)
                if upd is None:
                    continue
                try:
                    await dp.feed_update(max_bot, upd)
                except Exception:
                    log.exception("MAX: ошибка обработки обновления %s", u.get("update_type"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("MAX: нет связи (%s), повтор через %s с", e, pause)
            await asyncio.sleep(pause)
            pause = min(pause * 2, 60)


async def start(token: str, tg_bot: Bot, dp, session: MaxSession | None = None) -> list:
    """Подключить MAX к общему сценарию. Возвращает задачу для asyncio.gather.
    Если MAX при запуске недоступен — не сдаёмся: пробуем снова, пока не получится."""
    session = session or MaxSession(token)
    session.tg_bot = tg_bot
    max_bot = Bot("1:max", session=session)  # токен-заглушка: настоящий передаётся в заголовке Authorization
    if isinstance(tg_bot.session, RoutingSession):
        tg_bot.session.max_session, tg_bot.session.max_bot = session, max_bot
    from . import notify
    notify.max_bot = max_bot

    async def run():
        pause = 5
        while True:
            try:
                me = await max_bot.get_me()
                break
            except Exception as e:
                hint = " (неверный токен?)" if "401" in str(e) or "verify.token" in str(e) else ""
                log.warning("MAX-бот пока не подключился%s: %s — повтор через %s с", hint, e, pause)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 300)
        runtime["max_username"] = me.username or ""
        try:
            from .bot import setup_commands
            await setup_commands(max_bot)
        except Exception as e:
            log.warning("MAX: не удалось установить команды: %s", e)
        log.info("MAX-бот подключён: https://max.ru/%s", runtime["max_username"])
        from .applog import events
        events.info("🟣 MAX-бот подключён: https://max.ru/%s", runtime["max_username"])
        await poll(dp, max_bot, session)

    return [run()]
