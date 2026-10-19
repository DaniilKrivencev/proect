"""Настройки приложения. Все значения берутся из файла .env рядом с run.bat."""
import os
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
PORT = int(os.getenv("PORT", "8080"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_PATH = TENANT_DIR / os.getenv("DB_FILE", "lunka.db")
# Только для проверки кабинета в обычном браузере без Telegram. В работе оставить 0.
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "0") or 0)

# Служебный бот, который присылает логи разработчику. Пусто — логи только в окне.
LOG_BOT_TOKEN = os.getenv("LOG_BOT_TOKEN", "").strip()
# Бот салона в MAX (необязательно). Токен — на business.max.ru → Чат-боты → Расширенные настройки
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
# Прокси для Telegram (на российских серверах Telegram заблокирован). Пусто — напрямую.
# Пример: socks5://127.0.0.1:1080 — SSH-мост до зарубежного сервера (см. МОСТ_ДЛЯ_TELEGRAM.txt)
TG_PROXY = os.getenv("TG_PROXY", "").strip() or None
# Кому бот логов разрешает подключиться (username в Telegram без @)
LOG_OWNER = os.getenv("LOG_OWNER", "daniilkrivencev").strip().lstrip("@").lower()

BOOK_DAYS_AHEAD = 14      # на сколько дней вперёд можно записаться
MIN_LEAD_MINUTES = 60      # не раньше чем через час от текущего времени
SLEEPING_CLIENT_DAYS = 45  # «давно не был» — после стольких дней без визита
DIGEST_HOUR = 20           # во сколько мастеру приходит план на завтра

# Значения, которые меняются во время работы (адрес туннеля, имя бота)
runtime = {"webapp_url": WEBAPP_URL, "bot_username": ""}
