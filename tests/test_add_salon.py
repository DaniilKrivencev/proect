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

adminbot.SHARED_DIR = Path(tempfile.mkdtemp())  # PIN и id — во временную папку, не в проект

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
s3 = M.create_tenant('Студия Лиса', 'custom', '333:CCC', '', 'Массаж и SPA')
c = M.tenants()[s3]; print(s3, c['NICHE'], c['NICHE_TITLE'], c['NICHE_EMOJI'])
M.set_disabled(s3, True); print(M.tenants()[s3].get('DISABLED'))
M.set_disabled(s3, False); print(M.tenants()[s3].get('DISABLED', '-'))
print(M.archive_tenant(s3).parent.name, s3 in M.tenants())
"""
out = subprocess.run([sys.executable, "-c", code], cwd=work, capture_output=True, text=True, check=True).stdout.split("\n")
assert out[0] == "nogti-u-anny barber-room nogti-u-anny-2 salon", out[0]
assert out[1] == "nogti-u-anny 8101 Ногти у Анны", out[1]
assert out[2] == "dup-ok"
assert out[3] == "nogti-u-anny-2 hair 8102", out[3]
assert out[4] == "studiya-lisa custom Массаж и SPA 💆", out[4]
assert out[5] == "1" and out[6] == "-", out[5:7]
assert out[7] == "archive False", out[7]
# салон своего типа запускается с правильными словами и без стартового прайса
env = dict(os.environ, LUNKA_TENANT_DIR=str(work / "tenants" / "nogti-u-anny"), NICHE="custom", NICHE_TITLE="брови и ресницы", NICHE_EMOJI="")
n = subprocess.run([sys.executable, "-c", "from app.niche import N, KEY; print(KEY, N['emoji'], N['title'], len(N['services']))"],
                   cwd=work, env=env, capture_output=True, text=True, check=True).stdout.strip()
assert n == "custom ✨ брови и ресницы 0", n
assert (work / "tenants" / "nogti-u-anny" / "lunka.db").exists(), "название сразу записано в базу салона"


# 2. Диалог в боте техподдержки
async def main():
    session = FakeSession()
    bot = Bot("123:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(adminbot.router)
    created, started = [], []
    adminbot.CREATE_SALON = lambda name, niche, token, mx, title="": created.append((name, niche, token, mx)) or "nogti-u-anny"

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
    # 3. Свой тип салона
    created.clear()
    await dp.feed_update(bot, cb(DEV, "dv:add"))
    await dp.feed_update(bot, msg(DEV, "Студия Лиса"))
    await dp.feed_update(bot, cb(DEV, "dv:niche_own"))
    await dp.feed_update(bot, msg(DEV, "Массаж и SPA"))
    assert "💆 Массаж и SPA" in session.sent[-1][2], session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "123456:GOODTOKEN_xxxxxxxxxxxxxxxxxxxxxx"))
    await dp.feed_update(bot, cb(DEV, "dv:max_skip"))
    assert created and created[0][1] == "custom", created

    # 4. Отключение / удаление — только с PIN; 3 неверных — блокировка; удаление — ещё и название
    t = Path(tempfile.mkdtemp()) / "tenants" / "lisa"
    t.mkdir(parents=True)
    (t / "tenant.env").write_text("NAME=Студия Лиса\nBOT_TOKEN=1:a\nPORT=8101\n", encoding="utf-8")
    adminbot.TENANTS = t.parent
    log_ = []
    adminbot.DISABLE_SALON = lambda slug: log_.append(("off", slug))

    async def enable(slug):
        log_.append(("on", slug))
    adminbot.ENABLE_SALON = enable
    adminbot.REMOVE_SALON = lambda slug: log_.append(("del", slug)) or "lisa_2026"
    await dp.feed_update(bot, cb(777, "dv:off:lisa"))  # посторонний
    await dp.feed_update(bot, cb(DEV, "dv:off:lisa"))
    assert "придумайте PIN" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "12"))
    assert "от 4 до 8 цифр" in session.sent[-1][2]
    await dp.feed_update(bot, msg(DEV, "4815"))
    assert adminbot.pin_is_set() and "4815" not in (adminbot.SHARED_DIR / "admin.json").read_text(), "PIN не хранится открыто"
    await dp.feed_update(bot, msg(DEV, "4815"))
    assert log_ == [("off", "lisa")], log_
    # неверный PIN ×3 → блокировка
    for _ in range(3):
        await dp.feed_update(bot, cb(DEV, "dv:del:lisa"))
        await dp.feed_update(bot, msg(DEV, "0000"))
    assert "заблокированы на 15 минут" in session.sent[-1][2]
    await dp.feed_update(bot, cb(DEV, "dv:del:lisa"))
    assert "Попробуйте через" in session.sent[-1][2]
    adminbot._fails.update(n=0, until=0)
    # удаление: верный PIN, но неверное название — отмена
    await dp.feed_update(bot, cb(DEV, "dv:del:lisa"))
    await dp.feed_update(bot, msg(DEV, "4815"))
    await dp.feed_update(bot, msg(DEV, "лиса"))
    assert "не совпало" in session.sent[-1][2] and ("del", "lisa") not in log_
    await dp.feed_update(bot, cb(DEV, "dv:del:lisa"))
    await dp.feed_update(bot, msg(DEV, "4815"))
    await dp.feed_update(bot, msg(DEV, "Студия Лиса"))
    assert ("del", "lisa") in log_ and "archive/lisa_2026" in session.sent[-1][2]
    assert not [s_ for s_ in session.sent if "4815" in s_[2]], "PIN не должен появляться в ответах"

    print("✅ Добавление салона из бота: 4 шага, проверка токена, запуск без перезагрузки, адреса латиницей")


asyncio.run(main())
