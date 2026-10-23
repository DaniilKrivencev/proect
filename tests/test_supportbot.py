"""Бот поддержки: обращение → оператору, ответ свайпом → человеку; кнопка в ботах салонов. python -m tests.test_supportbot"""
import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="123:TEST", DB_FILE=os.path.join(tmp, "t.db"), TUNNEL="off", LUNKA_TENANT="zarina")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, MessageId, Update, User

from app import config, supportbot

SHARED = Path(tmp) / "shared"
SHARED.mkdir()
supportbot.SHARED_DIR = config.SHARED_DIR = SHARED
supportbot.TENANTS = Path(tmp) / "tenants"
(supportbot.TENANTS / "zarina").mkdir(parents=True)
(supportbot.TENANTS / "zarina" / "tenant.env").write_text("NAME=Стиль и Я\n", encoding="utf-8")
(SHARED / "admin.json").write_text(json.dumps({"dev_id": 555}), encoding="utf-8")
OP, USER = 555, 777


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent, self.n = [], 100

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.n += 1
        self.sent.append((name, getattr(method, "chat_id", None), getattr(method, "text", None),
                          getattr(method, "reply_to_message_id", None)))
        if name == "CopyMessage":
            return MessageId(message_id=self.n)
        if name == "SendMessage":
            return Message(message_id=self.n, date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""


def msg(uid, text, reply_to=None, mid=5):
    rt = Message(message_id=reply_to, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x") if reply_to else None
    return Update(update_id=1, message=Message(message_id=mid, date=datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=User(id=uid, is_bot=False, first_name="Зарина", username=f"u{uid}"), text=text,
                  reply_to_message=rt))


async def main():
    s = FakeSession()
    bot = Bot("123:TEST", session=s, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(supportbot.router)

    # Кнопка в салоне появляется, когда менеджер записал адрес бота
    assert config.support_url("owner") == ""
    supportbot._save_info(username="podderzhka_salon_bot")
    assert config.support_url("owner") == "https://t.me/podderzhka_salon_bot?start=zarina-owner"

    await dp.feed_update(bot, msg(USER, "/start zarina-owner"))
    assert "Стиль и Я" in s.sent[-1][2]
    s.sent.clear()
    await dp.feed_update(bot, msg(USER, "Не открывается кабинет"))
    head = [x for x in s.sent if x[0] == "SendMessage" and x[1] == OP][0]
    assert "Стиль и Я" in head[2] and "владелец" in head[2] and "@u777" in head[2], head
    copy = [x for x in s.sent if x[0] == "CopyMessage"][0]
    assert copy[1] == OP
    assert any(x[1] == USER and "Получил" in (x[2] or "") for x in s.sent), "нет подтверждения"
    copy_id = s.n - 1  # id копии у оператора (до подтверждения)

    # Ответ оператора свайпом уходит человеку
    s.sent.clear()
    await dp.feed_update(bot, msg(OP, "Перезапустите бота: /start", reply_to=copy_id))
    assert [x for x in s.sent if x[0] == "CopyMessage" and x[1] == USER], s.sent
    # Без свайпа — подсказка, никому не уходит
    s.sent.clear()
    await dp.feed_update(bot, msg(OP, "просто текст"))
    assert not [x for x in s.sent if x[0] == "CopyMessage"] and "свайпните" in s.sent[-1][2]
    # Чужая роль в ссылке не ломает бота
    await dp.feed_update(bot, msg(888, "/start ../etc-owner"))
    print("✅ Бот поддержки: обращения доходят оператору с салоном и ролью, ответ свайпом возвращается человеку")


asyncio.run(main())
