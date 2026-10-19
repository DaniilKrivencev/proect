"""Запуск ОДНОГО салона: бот, кабинет и напоминания.

Обычно его запускает менеджер (manager.py) — по отдельному процессу на каждый салон,
каждый со своей папкой tenants/<салон>/ и своей базой. Можно запустить и напрямую (старый режим).
"""
import asyncio
import logging
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app import api, applog, bot as bot_module, config, db, maxbot, notify, reminders, tunnel

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s", datefmt="%H:%M:%S")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
log = logging.getLogger("lunka")


def fail(msg: str):
    print("\n" + "=" * 60 + f"\nОШИБКА: {msg}\n" + "=" * 60)
    sys.exit(1)


def ask_token():
    """Первый запуск: спросить токен в окне и сохранить в .env."""
    print("\nПервый запуск. Вставьте токен бота от @BotFather")
    print("(правая кнопка мыши в этом окне = вставить) и нажмите Enter:\n")
    token = input("Токен: ").strip().strip('"')
    if ":" not in token:
        fail("Это не похоже на токен. Он выглядит так: 1234567890:AAH...")
    env = config.BASE_DIR / ".env"
    template = config.BASE_DIR / ".env.example"
    text = template.read_text(encoding="utf-8") if template.exists() else "BOT_TOKEN=\n"
    if "BOT_TOKEN=" in text:
        lines = [f"BOT_TOKEN={token}" if l.startswith("BOT_TOKEN=") else l for l in text.splitlines()]
        text = "\n".join(lines) + "\n"
    else:
        text = f"BOT_TOKEN={token}\n" + text
    env.write_text(text, encoding="utf-8")
    config.BOT_TOKEN = token
    print("Токен сохранён в файл .env — больше спрашивать не буду.\n")


def ask_log_token():
    """Один раз спросить токен служебного бота логов (можно пропустить Enter)."""
    env = config.BASE_DIR / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    if any(l.startswith("LOG_BOT_TOKEN=") for l in text.splitlines()):
        return  # уже спрашивали (пустое значение = отказались)
    print("\nНеобязательно: токен бота для логов (присылает ошибки и события разработчику).")
    print("Вставьте токен и нажмите Enter, или просто нажмите Enter, чтобы пропустить:\n")
    token = input("Токен бота логов: ").strip().strip('"')
    if token and ":" not in token:
        print("Это не похоже на токен — пропускаю. Можно вписать позже в .env: LOG_BOT_TOKEN=...")
        token = ""
    env.write_text(text.rstrip("\n") + f"\nLOG_BOT_TOKEN={token}\n", encoding="utf-8")
    config.LOG_BOT_TOKEN = applog.LOG_BOT_TOKEN = token
    print("Сохранено.\n" if token else "Пропущено.\n")


async def main():
    managed = bool(config.TENANT)  # запущен менеджером — ничего не спрашиваем в окне
    if not config.BOT_TOKEN or ":" not in config.BOT_TOKEN:
        if managed:
            fail(f"У салона «{config.TENANT}» не указан BOT_TOKEN в tenants/{config.TENANT}/tenant.env")
        ask_token()
    if not config.LOG_BOT_TOKEN and not managed:
        ask_log_token()

    db.init()
    applog.install()

    if not config.runtime["webapp_url"] and config.TUNNEL == "auto":
        log.info("Запускаю туннель для кабинета мастера…")
        url = await asyncio.to_thread(tunnel.start, config.PORT)
        if url:
            config.runtime["webapp_url"] = url

    from app import api as _api
    _api.BOT_TOKEN = config.BOT_TOKEN
    # Сессия-маршрутизатор: сообщения клиентам из MAX (id < 0) сами уходят в MAX
    bot = Bot(config.BOT_TOKEN, session=maxbot.RoutingSession(proxy=config.TG_PROXY),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception as e:
        hint = ("Проверьте мост: systemctl status tg-bridge" if config.TG_PROXY else
                "Если сервер в России — Telegram отсюда заблокирован, нужен мост: МОСТ_ДЛЯ_TELEGRAM.txt")
        fail(f"Telegram не принял токен или нет интернета: {e}\n{hint}")
    config.runtime["bot_username"] = me.username
    if config.TG_PROXY:
        log.info("Telegram работает через мост %s", config.TG_PROXY)
    notify.bot = bot

    # Обновить кнопку «Кабинет» у владельца и мастеров (адрес туннеля мог смениться)
    await bot_module.refresh_staff_buttons(bot)
    try:
        await bot_module.setup_commands(bot)
    except Exception as e:
        log.warning("Не удалось обновить меню команд: %s", e)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(bot_module.router)

    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=config.PORT, log_level="warning"))

    print("\n" + "=" * 60)
    print(f"  ✅ Бот запущен: https://t.me/{me.username}")
    print(f"  Кабинет мастера: {config.runtime['webapp_url'] or 'недоступен (нет туннеля)'}")
    print("  Не закрывайте это окно, пока бот должен работать.")
    print("=" * 60 + "\n")

    applog.events.info("🟢 Запущен. Бот @%s, кабинет: %s", me.username,
                       config.runtime["webapp_url"] or "нет туннеля")
    tasks = [dp.start_polling(bot, handle_signals=False), server.serve(), reminders.loop(), applog.run(listen=not managed)]
    if config.MAX_BOT_TOKEN:  # тот же сценарий — в MAX
        tasks += await maxbot.start(config.MAX_BOT_TOKEN, bot, dp)
    if config.TUNNEL == "auto" and not config.WEBAPP_URL:
        tasks.append(tunnel_watchdog(bot))
    await asyncio.gather(*tasks)


async def tunnel_watchdog(bot):
    """Если туннель упал (сон компьютера, обрыв сети) — поднять заново и обновить кнопки «Кабинет»."""
    while True:
        await asyncio.sleep(60)
        if tunnel.alive():
            continue
        log.warning("Туннель кабинета не работает — перезапускаю…")
        url = await asyncio.to_thread(tunnel.start, config.PORT)
        config.runtime["webapp_url"] = url or ""
        await bot_module.refresh_staff_buttons(bot)
        if url:
            applog.events.info("🔁 Кабинет снова доступен: %s", url)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        applog.send_sync("🔴 Остановлен вручную (Ctrl+C).")
        print("Остановлено.")
    except SystemExit:
        raise
    except BaseException as e:
        import traceback, html as _h
        tb = traceback.format_exc()
        applog.send_sync(f"🔴 <b>Упал</b>\n<code>{_h.escape(tb[-3000:])}</code>")
        raise
