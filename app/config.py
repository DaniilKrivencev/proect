"""Настройки приложения. Все значения берутся из файла .env рядом с run.bat."""
import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # папка с кодом (общая для всех салонов)
# Папка конкретного салона: tenants/<имя>/ — там его tenant.env, база и логи туннеля.
# Менеджер (manager.py) задаёт её каждому процессу. Без менеджера — старый режим: всё в корне.
TENANT_DIR = Path(os.getenv("LUNKA_TENANT_DIR") or BASE_DIR).resolve()
TENANT = os.getenv("LUNKA_TENANT", "") or ("" if TENANT_DIR == BASE_DIR else TENANT_DIR.name)
load_dotenv(TENANT_DIR / "tenant.env")   # настройки салона (BOT_TOKEN, NICHE, PORT) — важнее общих
load_dotenv(BASE_DIR / ".env")           # общие настройки (LOG_BOT_TOKEN и т.п.)
SHARED_DIR = BASE_DIR / "shared"         # общее для менеджера и салонов (например, чат бота логов)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Адрес кабинета мастера (https). Если пусто и TUNNEL=auto — создаётся автоматически.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
TUNNEL = os.getenv("TUNNEL", "auto").strip().lower()
# Свой адрес на сервере вместо туннеля: кабинет салона = https://<папка>.<DOMAIN>
# (сертификат и маршрут делает Caddy, см. КАБИНЕТ_ПО_ПРЯМОМУ_АДРЕСУ.txt)
DOMAIN = os.getenv("DOMAIN", "").strip().removeprefix("https://").removeprefix("http://").strip("/")
if not WEBAPP_URL and DOMAIN and TENANT:
    WEBAPP_URL = f"https://{TENANT}.{DOMAIN}"
PORT = int(os.getenv("PORT", "8080"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_PATH = TENANT_DIR / os.getenv("DB_FILE", "lunka.db")
# Только для проверки кабинета в обычном браузере без Telegram. В работе оставить 0.
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "0") or 0)

# Служебный бот, который присылает логи разработчику. Пусто — логи только в окне.
LOG_BOT_TOKEN = os.getenv("LOG_BOT_TOKEN", "").strip()
# Бот салона в MAX (необязательно). Токен — на business.max.ru → Чат-боты → Расширенные настройки
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
# Бот техподдержки (один на сервер): вход в любой салон как клиент/хозяин/мастер. Доступ — у LOG_OWNER.
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "").strip()
# Бот поддержки (один на сервер): кнопка «🆘 Поддержка» у владельцев, мастеров и клиентов всех салонов.
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "").strip()
# Прокси для Telegram (на российских серверах Telegram заблокирован). Пусто — напрямую.
# Пример: socks5://127.0.0.1:1080 — SSH-мост до зарубежного сервера (см. МОСТ_ДЛЯ_TELEGRAM.txt)
TG_PROXY = os.getenv("TG_PROXY", "").strip() or None
# Или свой адрес вместо api.telegram.org — мост на Cloudflare Workers, сервер не нужен.
# Пример: https://lunka-tg.имя.workers.dev
TG_API = os.getenv("TG_API", "").strip().rstrip("/")


def tg_session(cls=None):
    """Сессия для Telegram-бота с учётом моста (TG_API / TG_PROXY)."""
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    s = (cls or AiohttpSession)(proxy=TG_PROXY)
    if TG_API:
        s.api = TelegramAPIServer.from_base(TG_API)
    return s
# Как долго бот ждёт новых сообщений в одном запросе к Telegram (максимум 50 с). Ответы при этом мгновенные —
# Telegram прерывает ожидание, как только кто-то написал. Чем больше — тем меньше запросов через мост.
POLL_TIMEOUT = 50
# Кому бот логов разрешает подключиться (username в Telegram без @)
LOG_OWNER = os.getenv("LOG_OWNER", "daniilkrivencev").strip().lstrip("@").lower()

BOOK_DAYS_AHEAD = 14      # на сколько дней вперёд можно записаться
MIN_LEAD_MINUTES = 60      # не раньше чем через час от текущего времени
SLEEPING_CLIENT_DAYS = 45  # «давно не был» — после стольких дней без визита
DIGEST_HOUR = 20           # во сколько мастеру приходит план на завтра

# Значения, которые меняются во время работы (адрес туннеля, имя бота)
runtime = {"webapp_url": WEBAPP_URL, "bot_username": ""}


# ---------- Короткие сообщения о сбоях моста ----------
# Когда Cloudflare/Telegram на секунду отвечает ошибкой (520, 502…), aiogram печатает в окно
# всю HTML-страницу ошибки (сотни строк). Оставляем одну понятную строку — бот сам переподключается.
class _ShortHtmlErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "<!DOCTYPE" in msg or "<html" in msg:
            code = re.search(r"Error code (\d{3})|\b(5\d\d):", msg)
            num = next((g for g in (code.groups() if code else ()) if g), "")
            record.msg = ("Failed to fetch updates: Telegram-мост на секунду ответил ошибкой" + (f" {num}" if num else "") +
                          " — бот переподключится сам, ничего делать не нужно")
            record.args = ()
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True


logging.getLogger("aiogram.dispatcher").addFilter(_ShortHtmlErrors())


# ---------- Кнопка «🆘 Поддержка» ----------
_support = {"mtime": None, "user": ""}


def support_url(role: str) -> str:
    """Ссылка на общий бот поддержки: t.me/<бот>?start=<салон>-<роль>. Адрес бота менеджер пишет в shared/support.json."""
    f = SHARED_DIR / "support.json"
    try:
        mt = f.stat().st_mtime
        if mt != _support["mtime"]:
            import json
            _support.update(mtime=mt, user=json.loads(f.read_text(encoding="utf-8")).get("username") or "")
    except Exception:
        _support.update(mtime=None, user="")
    if not _support["user"]:
        return ""
    return f"https://t.me/{_support['user']}?start={TENANT or 'salon'}-{role}"
