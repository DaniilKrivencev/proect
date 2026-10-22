"""Добавление салона из бота техподдержки: python -m tests.test_add_salon"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off", LOG_OWNER="daniilkrivencev")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import adminbot

DEV = 555


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if text:
            self.sent.append((getattr(method, "chat_id", None), name, text, getattr(method, "reply_markup", None)))
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Лунка", username="lunka_test_bot")
        if name in ("SendMessage", "EditMessageText"):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"), text=text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""


def user(uid, name="Анна"):
    return User(id=uid, is_bot=False, first_name=name, username=f"user{uid}")


def msg(uid, text=None, contact=None, photo=None):
    return Update(update_id=1, message=Message(message_id=5, date=datetime.now(), chat=Chat(id=uid, type="private"),
                                               from_user=user(uid), text=text, contact=contact, photo=photo))


def cb(uid, data):
    return Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=user(uid), chat_instance="x", data=data,
        message=Message(message_id=7, date=datetime.now(), chat=Chat(id=uid, type="private"), text="old")))
JPEG = b"\xff\xd8\xff\xe0fakejpeg"



def user(uid, name="Даниил"):  # noqa: F811 — нужен username владельца платформы
    return User(id=uid, is_bot=False, first_name=name, username="daniilkrivencev" if uid == DEV else f"u{uid}")


# 1. Транслитерация и создание салона — на копии проекта
SRC = Path(__file__).resolve().parent.parent
work = Path(tempfile.mkdtemp()) / "p"
shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "tenants", "backups", "shared", ".venv"))
code = """
import manager as M
print(M.slugify('Ногти у Анны', {}), M.slugify('Barber Room!', {}), M.slugify('Ногти у Анны', {'nogti-u-anny': 1}), M.slugify('💅💅', {}))
s = M.create_tenant('Ногти у Анны', 'nails', '111:AAA', '')
print(s, M.tenants()[s]['PORT'], M.tenants()[s]['NAME'])
try:
    M.create_tenant('Другой', 'hair', '111:AAA')
except ValueError as e:
    print('dup-ok')
s2 = M.create_tenant('Ногти у Анны', 'hair', '222:BBB')
print(s2, M.tenants()[s2]['NICHE'], M.tenants()[s2]['PORT'])
"""
out = subprocess.run([sys.executable, "-c", code], cwd=work, capture_output=True, text=True, check=True).stdout.split("\n")
assert out[0] == "nogti-u-anny barber-room nogti-u-anny-2 salon", out[0]
assert out[1] == "nogti-u-anny 8101 Ногти у Анны", out[1]
assert out[2] == "dup-ok"
assert out[3] == "nogti-u-anny-2 hair 8102", out[3]
assert (work / "tenants" / "nogti-u-anny" / "lunka.db").exists(), "название сразу записано в базу салона"


# 2. Диалог в боте техподдержки
async def main():
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(adminbot.router)
    created, started = [], []
    adminbot.CREATE_SALON = lambda name, niche, token, mx: created.append((name, niche, token, mx)) or "nogti-u-anny"

    async def start(slug):
        started.append(slug)
        return "https://nogti-u-anny.example.ru"
    adminbot.START_SALON = start

    async def check(token):
        if token != "123456:GOODTOKEN_xxxxxxxxxxxxxxxxxxxxxx":
            raise RuntimeError("bad")
        return "nogti_anny_bot"
    adminbot._check_tg_token = check

    # посторонний не может
    await dp.feed_update(bot, cb(777, "dv:add"))
    assert not session.sent
    await dp.feed_update(bot, cb(DEV, "dv:add"))
    assert "шаг 1 из 4" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "Ногти у Анны"))
    assert "Шаг 2 из 4" in session.sent[-1][2]
    await dp.feed_update(bot, cb(DEV, "dv:niche:nails"))
    assert "Шаг 3 из 4" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "привет"))
    assert "не похоже на токен" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "999:WRONG_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"))
    assert "не принял" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "123456:GOODTOKEN_xxxxxxxxxxxxxxxxxxxxxx"))
    assert "@nogti_anny_bot подходит" in session.sent[-1][2]
    await dp.feed_update(bot, cb(DEV, "dv:max_skip"))
    assert created == [("Ногти у Анны", "nails", "123456:GOODTOKEN_xxxxxxxxxxxxxxxxxxxxxx", "")], created
    assert started == ["nogti-u-anny"]
    final = session.sent[-1][2]
    assert "запущен" in final and "https://t.me/nogti_anny_bot" in final and "nogti-u-anny.example.ru" in final
    assert not any("GOODTOKEN" in s[2] for s in session.sent), "токен не должен попадать в ответы бота"
    print("✅ Добавление салона из бота: 4 шага, проверка токена, запуск без перезагрузки, адреса латиницей")


asyncio.run(main())
