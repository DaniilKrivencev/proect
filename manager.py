"""Менеджер салонов: запускает ВСЕ салоны на одном компьютере/сервере и следит, чтобы они работали.

Как устроено:
  tenants/
    zarina/              ← салон Зарины (маникюр)
      tenant.env         ← её бот (BOT_TOKEN), тип салона (NICHE), порт
      lunka.db           ← ЕЁ база: клиенты, записи, мастера. Больше ничья.
    masha/               ← салон Маши (парикмахерская)
      tenant.env
      lunka.db

• Каждый салон — отдельный процесс со своей базой: данные физически не могут смешаться.
• Упал один салон — остальные работают, а менеджер перезапускает упавший (5 с → 10 с → 30 с → 60 с).
• Все пишут в один бот логов с пометкой [имя салона].

Команды:
  python manager.py          — запустить все салоны
  python manager.py add      — добавить салон (спросит имя, тип и токен бота)
  python manager.py list     — показать салоны
  python manager.py max      — подключить салону бота в MAX
  python manager.py backup   — сделать копию всего сервиса (для переезда и на всякий случай)
  python manager.py restore backups/backup_….zip — развернуть копию (на новом сервере)
  python manager.py rename salon1 zarina — переименовать папку салона
  python manager.py reset    — очистить сервис от тестовых данных (перед выкладкой на сервер)
  python manager.py import zarina salons/zarina.json — заполнить салон: название, прайс, мастера
"""
import asyncio
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TENANTS = BASE / "tenants"
SHARED = BASE / "shared"
FIRST_PORT = 8101
NICHES = {"1": ("nails", "салон маникюра"), "2": ("hair", "парикмахерская")}

os.environ.setdefault("PYTHONUTF8", "1")


# ======================================================================
#  Настройки салонов
# ======================================================================
def read_env(path: Path) -> dict:
    data = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"')
    return data


def write_tenant(slug: str, cfg: dict):
    d = TENANTS / slug
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# Настройки салона. Токены — секрет, никому не показывайте.",
             f"NAME={cfg['NAME']}", f"NICHE={cfg['NICHE']}", f"BOT_TOKEN={cfg['BOT_TOKEN']}", f"PORT={cfg['PORT']}",
             "# Бот в MAX (необязательно): токен с business.max.ru → Чат-боты → Расширенные настройки",
             f"MAX_BOT_TOKEN={cfg.get('MAX_BOT_TOKEN', '')}"]
    for k, v in cfg.items():
        if k not in ("NAME", "NICHE", "BOT_TOKEN", "PORT", "MAX_BOT_TOKEN"):
            lines.append(f"{k}={v}")
    (d / "tenant.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tenants() -> dict[str, dict]:
    out = {}
    if TENANTS.exists():
        for d in sorted(TENANTS.iterdir()):
            if d.is_dir() and (d / "tenant.env").exists():
                out[d.name] = read_env(d / "tenant.env")
    return out


def write_caddy(ts: dict[str, dict]):
    """Если в .env указан DOMAIN — составить Caddyfile: <папка>.<DOMAIN> → порт салона, и перезагрузить Caddy."""
    from dotenv import dotenv_values
    domain = (dotenv_values(BASE / ".env").get("DOMAIN") or "").strip()
    domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
    if not domain:
        return
    blocks = [f"{slug}.{domain} {{\n    reverse_proxy 127.0.0.1:{c.get('PORT')}\n}}\n" for slug, c in ts.items()]
    path = BASE / "Caddyfile"
    text = "# Создаётся автоматически при запуске manager.py — руками не править\n\n" + "\n".join(blocks)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        if shutil.which("systemctl") and shutil.which("caddy"):
            import subprocess
            r = subprocess.run(["systemctl", "reload", "caddy"], capture_output=True, text=True)
            print("Адреса кабинетов обновлены в Caddy." if r.returncode == 0 else
                  f"Не удалось перезагрузить Caddy: {r.stderr.strip()}")
    for slug in ts:
        print(f"  Кабинет {slug}: https://{slug}.{domain}")


def problems(ts: dict[str, dict]) -> list[str]:
    """Проверка, что салоны не мешают друг другу."""
    errs, tokens, ports, max_tokens = [], {}, {}, {}
    for slug, c in ts.items():
        mt = c.get("MAX_BOT_TOKEN", "")
        if mt:
            if mt in max_tokens:
                errs.append(f"{slug} и {max_tokens[mt]} используют один и тот же бот MAX — у каждого салона должен быть свой")
            max_tokens[mt] = slug
        tok, port = c.get("BOT_TOKEN", ""), c.get("PORT", "")
        if ":" not in tok:
            errs.append(f"{slug}: не указан токен бота (BOT_TOKEN) в tenants/{slug}/tenant.env")
        elif tok in tokens:
            errs.append(f"{slug} и {tokens[tok]} используют ОДИН И ТОТ ЖЕ бот — у каждого салона должен быть свой")
        else:
            tokens[tok] = slug
        if port in ports:
            errs.append(f"{slug} и {ports[port]} используют один порт {port}")
        ports[port] = slug
    return errs


def import_salon(slug: str, file: str):
    """Заполнить салон из файла: название, прайс, мастера и их услуги (salons/<салон>.json).
    Можно запускать повторно: услуги и мастера обновляются по названию/имени, записи и клиенты не трогаются."""
    import secrets
    from datetime import datetime
    cfg = tenants().get(slug)
    if not cfg:
        print(f"Салона «{slug}» нет. Список: python manager.py list")
        return
    dbf = TENANTS / slug / cfg.get("DB_FILE", "lunka.db")
    if not dbf.exists():
        print("База салона ещё не создана — запустите сервис хотя бы один раз, потом повторите.")
        return
    data = json.loads(Path(file).read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    con = sqlite3.connect(dbf, timeout=30)
    con.row_factory = sqlite3.Row
    hidden = 0
    with con:
        if data.get("salon_name"):
            con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('salon_name', ?)", (data["salon_name"],))
        # услуги: новые добавить, существующие обновить, лишние скрыть (история записей сохраняется)
        names, by_cat, by_name = set(), {}, {}
        for name, price, dur, cat in data["services"]:
            names.add(name)
            row = con.execute("SELECT id FROM services WHERE name=?", (name,)).fetchone()
            if row:
                sid = row["id"]
                con.execute("UPDATE services SET price=?, duration=?, price_from=1, active=1 WHERE id=?", (price, dur, sid))
            else:
                sid = con.execute("INSERT INTO services (name, price, duration, price_from, active) VALUES (?,?,?,1,1)",
                                  (name, price, dur)).lastrowid
            by_cat.setdefault(cat, []).append(sid)
            by_name[name] = sid
        for r in con.execute("SELECT id, name FROM services WHERE active=1").fetchall():
            if r["name"] not in names:
                con.execute("UPDATE services SET active=0 WHERE id=?", (r["id"],))
                hidden += 1
        # мастера: по имени; новым — график пн–пт 10–20, сб 10–18 (поменяете в кабинете)
        for m in data["masters"]:
            row = con.execute("SELECT id FROM masters WHERE name=? AND active=1", (m["name"],)).fetchone()
            if row:
                mid = row["id"]
                con.execute("UPDATE masters SET spec=? WHERE id=?", (m.get("spec", ""), mid))
            else:
                mid = con.execute("INSERT INTO masters (name, spec, invite, created_at) VALUES (?,?,?,?)",
                                  (m["name"], m.get("spec", ""), secrets.token_hex(5), now)).lastrowid
                for wd in range(6):
                    con.execute('INSERT OR REPLACE INTO schedule (master_id, weekday, start, "end") VALUES (?,?,?,?)',
                                (mid, wd, "10:00", "18:00" if wd == 5 else "20:00"))
            # услуги мастера: точным списком названий ("services") и/или целыми группами ("cats")
            sids = [sid for cat in m.get("cats", []) for sid in by_cat.get(cat, [])]
            for n in m.get("services", []):
                if n not in by_name:
                    raise SystemExit(f"У мастера {m['name']} услуга «{n}» не найдена в прайсе — проверьте название")
                sids.append(by_name[n])
            con.execute("DELETE FROM master_services WHERE master_id=?", (mid,))
            for sid in sids:
                con.execute("INSERT OR IGNORE INTO master_services (master_id, service_id) VALUES (?,?)", (mid, sid))
    con.close()
    if data.get("salon_name") and cfg.get("NAME") != data["salon_name"]:
        cfg["NAME"] = data["salon_name"]
        write_tenant(slug, cfg)
    print(f"✅ «{data.get('salon_name', slug)}»: услуг {len(data['services'])}, мастеров {len(data['masters'])}"
          + (f", старых услуг скрыто {hidden}" if hidden else "") + ".")
    print("   Ссылки-приглашения мастеров — в кабинете → «Мастера». График поправьте там же.")


def next_port(ts: dict[str, dict]) -> int:
    used = {int(c.get("PORT", 0) or 0) for c in ts.values()}
    p = FIRST_PORT
    while p in used:
        p += 1
    return p


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def add_tenant_interactive():
    ts = tenants()
    print("\n=== Новый салон ===")
    name = ask("Название салона (как увидят клиенты), например «Ногти у Зарины»: ") or "Новый салон"
    while True:
        slug = ask("Короткое имя папки латиницей, например zarina: ").lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}", slug) and slug not in ts:
            break
        print("  Только латинские буквы, цифры, - и _, и имя не должно повторяться.")
    print("Тип салона:  1 — маникюр   2 — парикмахерская")
    niche = NICHES.get(ask("Введите 1 или 2: "), NICHES["1"])[0]
    while True:
        token = ask("Токен НОВОГО бота от @BotFather (у каждого салона свой бот): ").strip('"')
        if ":" not in token:
            print("  Это не похоже на токен. Он выглядит так: 1234567890:AAH...")
            continue
        if any(c.get("BOT_TOKEN") == token for c in ts.values()):
            print("  Этот бот уже занят другим салоном. Создайте новый бот в @BotFather командой /newbot.")
            continue
        break
    print("Бот в MAX (необязательно). Токен берётся на business.max.ru → Чат-боты → Расширенные настройки.")
    max_token = ask("Токен бота MAX или просто Enter, чтобы пропустить: ").strip('"')
    if max_token and any(c.get("MAX_BOT_TOKEN") == max_token for c in ts.values()):
        print("  Этот бот MAX уже занят другим салоном — пропускаю. Можно вписать позже в tenant.env.")
        max_token = ""
    write_tenant(slug, {"NAME": name, "NICHE": niche, "BOT_TOKEN": token, "PORT": next_port(ts), "MAX_BOT_TOKEN": max_token})
    print(f"\n✅ Салон «{name}» добавлен в папку tenants/{slug}.")
    print("Запустите run.bat — и откройте нового бота: первым нажавший «Подключить салон» станет владельцем.\n")
    _set_salon_name(TENANTS / slug, name)


def connect_max_interactive():
    ts = tenants()
    if not ts:
        print("Сначала добавьте салон (добавить_салон.bat).")
        return
    print("\nСалоны:")
    slugs = list(ts)
    for i, slug in enumerate(slugs, 1):
        mark = "  (MAX уже подключён)" if ts[slug].get("MAX_BOT_TOKEN") else ""
        print(f"  {i} — {ts[slug].get('NAME', slug)} [{slug}]{mark}")
    choice = ask("Номер салона: ")
    if not choice.isdigit() or not 1 <= int(choice) <= len(slugs):
        print("Нет такого номера.")
        return
    slug = slugs[int(choice) - 1]
    print("Токен берётся на business.max.ru → Чат-боты → ваш бот → Расширенные настройки → Настроить.")
    token = ask("Токен бота MAX (пусто — отключить MAX у этого салона): ").strip('"')
    if token and any(c.get("MAX_BOT_TOKEN") == token for s, c in ts.items() if s != slug):
        print("Этот бот MAX уже подключён к другому салону. У каждого салона должен быть свой.")
        return
    cfg = dict(ts[slug])
    cfg["MAX_BOT_TOKEN"] = token
    write_tenant(slug, cfg)
    print(f"✅ Готово. Перезапустите run.bat — бот салона «{cfg.get('NAME', slug)}» заработает в MAX." if token
          else "MAX у этого салона отключён.")


def _set_salon_name(folder: Path, name: str):
    db = folder / "lunka.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('salon_name', ?)", (name,))
    con.commit()
    con.close()


def migrate_single_salon():
    """Перенести салон из старой версии (база и токен в корне папки) в tenants/<имя>/."""
    root_env = BASE / ".env"
    env = read_env(root_env)
    old_db = BASE / "lunka.db"
    if tenants() or not (old_db.exists() or ":" in env.get("BOT_TOKEN", "")):
        return
    slug = "salon1"
    print(f"\nНашёл салон из прошлой версии — переношу его в tenants/{slug}/ …")
    folder = TENANTS / slug
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = BASE / f"lunka.db{suffix}"
        if src.exists():
            shutil.move(str(src), str(folder / f"lunka.db{suffix}"))
    name, chat = "Салон", None
    if (folder / "lunka.db").exists():
        con = sqlite3.connect(folder / "lunka.db")
        try:
            rows = dict(con.execute("SELECT key, value FROM settings").fetchall())
            name, chat = rows.get("salon_name", name), rows.get("log_chat_id")
        except sqlite3.Error:
            pass
        con.close()
    write_tenant(slug, {"NAME": name, "NICHE": "nails", "BOT_TOKEN": env.get("BOT_TOKEN", ""), "PORT": FIRST_PORT})
    if chat:  # подключение бота логов из прошлой версии не теряем
        SHARED.mkdir(exist_ok=True)
        (SHARED / "logbot.json").write_text(json.dumps({"chat_id": int(chat)}), encoding="utf-8")
    if root_env.exists():  # токен салона теперь живёт в его папке
        text = root_env.read_text(encoding="utf-8")
        text = re.sub(r"^BOT_TOKEN=.*$", f"# BOT_TOKEN перенесён в tenants/{slug}/tenant.env", text, flags=re.M)
        root_env.write_text(text, encoding="utf-8")
    print(f"✅ Готово: «{name}» теперь в tenants/{slug}/ (тип: маникюр — можно поменять NICHE в tenant.env).\n")


def ask_log_token():
    env_path = BASE / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if any(l.startswith("LOG_BOT_TOKEN=") for l in text.splitlines()):
        return
    print("\nНеобязательно: токен бота для логов (присылает ошибки и события разработчику).")
    token = ask("Вставьте токен и Enter, или просто Enter, чтобы пропустить: ").strip('"')
    if token and ":" not in token:
        token = ""
    env_path.write_text(text.rstrip("\n") + f"\nLOG_BOT_TOKEN={token}\n", encoding="utf-8")
    os.environ["LOG_BOT_TOKEN"] = token


def ask_admin_token():
    env_path = BASE / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if any(l.startswith("ADMIN_BOT_TOKEN=") for l in text.splitlines()):
        return
    print("\nНеобязательно: токен бота техподдержки (вход в любой салон как клиент/хозяин/мастер).")
    owner = os.getenv("LOG_OWNER", "daniilkrivencev").strip().lstrip("@").lower()
    print(f"Доступ будет только у @{owner}. Создайте бота у @BotFather.")
    token = ask("Вставьте токен и Enter, или просто Enter, чтобы пропустить: ").strip('"')
    if token and ":" not in token:
        token = ""
    env_path.write_text(text.rstrip("\n") + f"\nADMIN_BOT_TOKEN={token}\n", encoding="utf-8")
    os.environ["ADMIN_BOT_TOKEN"] = token


# ======================================================================
#  Надзор за процессами салонов
# ======================================================================
# ======================================================================
#  Резервные копии, переезд на другой сервер, переименование салона
# ======================================================================
BACKUPS = BASE / "backups"
KEEP_BACKUPS = 14
CODE_ITEMS = ["app", "webapp", "tests", "main.py", "manager.py", "requirements.txt", "ИНСТРУКЦИЯ.txt",
              "ПЕРЕЕЗД_НА_ДРУГОЙ_СЕРВЕР.txt", "НОВЫЙ_ЗАКАЗЧИК.txt", ".env.example", ".gitignore", "salons"]


def make_backup(quiet: bool = False, out: Path | None = None) -> Path:
    """Один архив = весь сервис: код + базы и настройки ВСЕХ салонов + общие настройки.
    Базы копируются безопасно даже на работающем сервисе (встроенное резервирование SQLite)."""
    import zipfile
    BACKUPS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    out = out or BACKUPS / f"backup_{stamp}.zip"
    tmp = BACKUPS / f".tmp_{stamp}"
    tmp.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for item in CODE_ITEMS + [p.name for p in BASE.glob("*.bat")]:
                p = BASE / item
                if p.is_file():
                    z.write(p, item)
                elif p.is_dir():
                    for f in p.rglob("*"):
                        if f.is_file() and "__pycache__" not in f.parts:
                            z.write(f, f.relative_to(BASE).as_posix())
            for name in (".env",):
                if (BASE / name).exists():
                    z.write(BASE / name, name)
            if SHARED.exists():
                for f in SHARED.glob("*"):
                    if f.is_file():
                        z.write(f, f"shared/{f.name}")
            for slug in tenants():
                folder = TENANTS / slug
                z.write(folder / "tenant.env", f"tenants/{slug}/tenant.env")
                db_file = folder / "lunka.db"
                if db_file.exists():
                    snap = tmp / f"{slug}.db"
                    src = sqlite3.connect(db_file)
                    dst = sqlite3.connect(snap)
                    src.backup(dst)  # целостная копия, даже если салон сейчас работает
                    dst.close()
                    src.close()
                    z.write(snap, f"tenants/{slug}/lunka.db")
                for f in (folder / "reviews").glob("*.jpg"):  # фото к отзывам
                    z.write(f, f"tenants/{slug}/reviews/{f.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    old = sorted(BACKUPS.glob("backup_*.zip"))
    for f in old[:-KEEP_BACKUPS]:
        f.unlink(missing_ok=True)
    if not quiet:
        print(f"✅ Копия готова: {out}  ({out.stat().st_size // 1024} КБ)")
        print("   В ней токены ботов и данные клиентов — храните как пароль, никому не пересылайте.")
    return out


def restore_backup(path: str):
    """Развернуть архив из make_backup() в этой папке (на новом сервере)."""
    import zipfile
    src = Path(path).expanduser().resolve()
    if not src.exists():
        print(f"Файл не найден: {src}")
        sys.exit(1)
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        salons = sorted({n.split("/")[1] for n in names if n.startswith("tenants/") and n.count("/") >= 2})
        print(f"В копии салоны: {', '.join(salons) or 'нет'}")
        if tenants():
            keep = TENANTS.parent / f"tenants_old_{time.strftime('%Y-%m-%d_%H-%M')}"
            if ask(f"Здесь уже есть салоны ({', '.join(tenants())}). Заменить их данными из копии? "
                   f"Текущие будут сохранены в {keep.name}. Введите да: ").lower() not in ("да", "y", "yes"):
                print("Отменено.")
                return
            shutil.move(str(TENANTS), str(keep))
        for n in names:
            if n.endswith("/"):
                continue
            target = (BASE / n).resolve()
            if BASE not in target.parents:
                continue  # защита от «плохих» путей в архиве
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    for slug in tenants():  # остатки от прошлого места больше не нужны
        for junk in ("lunka.db-wal", "lunka.db-shm", "status.json"):
            (TENANTS / slug / junk).unlink(missing_ok=True)
    print("✅ Восстановлено. Теперь запустите сервис (run.bat или python manager.py).")


DATA_FILES = ("lunka.db", "lunka.db-wal", "lunka.db-shm", "status.json", "tunnel.log")


def reset_service(choice: str = "", salons: str = "", confirm: str = "", drop_backups: str = "") -> bool:
    """Очистка перед выкладкой на сервер. 1 — стереть данные салонов, оставить ботов; 2 — удалить салоны целиком.
    Перед этим — копия ВНЕ папки проекта (на уровень выше, обычно Рабочий стол)."""
    ts = tenants()
    print("\n=== Очистка сервиса ===")
    print("  1 — очистить данные (клиенты, записи, мастера, чаты, владелец), ОСТАВИТЬ ботов и их токены")
    print("  2 — полный сброс: удалить салоны вместе с ботами (пустой сервис как после установки)")
    choice = choice or ask("Введите 1 или 2: ")
    if choice not in ("1", "2"):
        print("Отменено.")
        return False
    if not ts and choice == "1":
        print("Салонов нет — очищать нечего.")
        return False
    targets = list(ts)
    if ts:
        print("\nСалоны: " + ", ".join(f"{s} ({c.get('NAME', '')})" for s, c in ts.items()))
        pick = (salons or ask("Какие очистить? Enter — все, или имена папок через пробел: ")).strip().lower()
        if pick and pick != "все":
            targets = [s for s in pick.split() if s in ts]
            if not targets:
                print("Таких салонов нет. Отменено.")
                return False
    what = "данные" if choice == "1" else "салоны ЦЕЛИКОМ (с ботами)"
    print(f"\nБудут удалены {what}: {', '.join(targets) or '—'}")
    if (confirm or ask("Чтобы подтвердить, напишите слово ОЧИСТИТЬ: ")).strip().upper() != "ОЧИСТИТЬ":
        print("Отменено, ничего не удалено.")
        return False

    safe = None
    if ts:
        safe = BASE.parent / f"lunka_копия_до_очистки_{time.strftime('%Y-%m-%d_%H-%M')}.zip"
        make_backup(quiet=True, out=safe)
        print(f"Копия всего, что было, сохранена ВНЕ проекта: {safe}")

    for slug in targets:
        folder = TENANTS / slug
        try:
            if choice == "1":
                for name in DATA_FILES:
                    (folder / name).unlink(missing_ok=True)
                shutil.rmtree(folder / "reviews", ignore_errors=True)
            else:
                shutil.rmtree(folder)
        except OSError:
            print(f"Не получилось очистить {slug} — сервис сейчас запущен. Закройте окно run.bat и повторите.")
            return False
    if choice == "2" and not tenants() and SHARED.exists():
        shutil.rmtree(SHARED, ignore_errors=True)  # подключение бота логов — заново через /start

    if BACKUPS.exists() and any(BACKUPS.glob("*.zip")):
        ans = drop_backups or ask("В папке backups лежат старые копии с тестовыми данными. Удалить их? (да/нет): ")
        if ans.strip().lower() in ("да", "y", "yes"):
            shutil.rmtree(BACKUPS, ignore_errors=True)
    for junk in BASE.glob("tenants_old_*"):
        shutil.rmtree(junk, ignore_errors=True)
    print("\n✅ Готово. Сервис чистый." + (" Боты остались — при запуске владелец снова нажимает «Подключить салон», "
                                         "прайс создастся стартовый." if choice == "1" else
                                         " Добавьте салон: добавить_салон.bat."))
    return True


def rename_tenant(old: str, new: str):
    if old not in tenants():
        print(f"Салона «{old}» нет. Есть: {', '.join(tenants()) or 'нет'}")
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}", new) or (TENANTS / new).exists():
        print("Новое имя: только латиница, цифры, - и _, и такой папки ещё не должно быть.")
        return
    try:
        (TENANTS / old).rename(TENANTS / new)
    except OSError:
        print("Не получилось — салон сейчас запущен. Закройте окно run.bat и повторите.")
        return
    print(f"✅ Папка салона теперь tenants/{new}. Ботов, базу и настройки это не меняет.")


async def nightly_backups(log, hour: int = 4):
    """Каждую ночь в 4:00 — автоматическая копия всего сервиса (хранятся последние 14)."""
    done = ""
    while True:
        await asyncio.sleep(300)
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour >= hour and done != today and not any(BACKUPS.glob(f"backup_{today}_*.zip")):
            try:
                path = await asyncio.to_thread(make_backup, True)
                print(f"Ночная копия: {path.name}", flush=True)
            except Exception as e:
                log(f"⚠️ Ночная резервная копия не создалась: {html.escape(str(e))}")
            done = today


class Runner:
    BACKOFF = [5, 10, 30, 60]

    def __init__(self, slug: str, cfg: dict, cmd: list[str] | None = None):
        self.slug, self.cfg = slug, cfg
        self.cmd = cmd or [sys.executable, "-u", str(BASE / "main.py")]
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.fast_fails = 0
        self.started_at = 0.0
        self.state = "запускается"
        self.stopping = False

    def env(self) -> dict:
        e = dict(os.environ)
        for k in ("BOT_TOKEN", "MAX_BOT_TOKEN", "PORT", "NICHE", "WEBAPP_URL", "DB_FILE"):
            e.pop(k, None)  # ничего не наследуем от соседей — только из своего tenant.env
        e.update(LUNKA_TENANT_DIR=str(TENANTS / self.slug), LUNKA_TENANT=self.slug,
                 PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        return e

    async def run(self, log):
        while not self.stopping:
            self.started_at = time.time()
            self.state = "работает"
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd, cwd=str(BASE), env=self.env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1 << 20)
            tail: list[str] = []
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                print(f"[{self.slug}] {line}", flush=True)
                tail = (tail + [line])[-15:]
            code = await self.proc.wait()
            if self.stopping:
                break
            lived = time.time() - self.started_at
            self.fast_fails = self.fast_fails + 1 if lived < 20 else 0
            self.restarts += 1
            if self.fast_fails >= 4:
                self.state = "остановлен: падает сразу после запуска"
                log(f"🛑 <b>[{self.slug}]</b> падает сразу после запуска 4 раза подряд — перестаю перезапускать. "
                    f"Нужна проверка (часто это неверный токен).\n<code>{html.escape(chr(10).join(tail[-8:]))}</code>")
                return
            delay = self.BACKOFF[min(self.fast_fails, len(self.BACKOFF) - 1)] if lived < 300 else self.BACKOFF[0]
            self.state = f"перезапуск через {delay} с"
            log(f"🔴 <b>[{self.slug}]</b> остановился (код {code}), перезапускаю через {delay} с. "
                f"Остальные салоны работают.\n<code>{html.escape(chr(10).join(tail[-6:]))}</code>")
            await asyncio.sleep(delay)

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass


def status_text(runners: dict[str, Runner]) -> str:
    parts = ["📊 <b>Все салоны</b>"]
    for slug, r in runners.items():
        st = {}
        try:
            st = json.loads((TENANTS / slug / "status.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        fresh = st and time.time() - st.get("updated", 0) < 120
        icon = "🟢" if r.state == "работает" and fresh else ("🟡" if r.state == "работает" else "🔴")
        line = f"\n{icon} <b>{html.escape(st.get('salon') or r.cfg.get('NAME', slug))}</b> [{slug}] — {r.state}"
        if st:
            line += (f"\n   @{st.get('bot') or '—'} · мастеров {st.get('masters', 0)} · клиентов {st.get('clients', 0)}"
                     f"\n   записей сегодня {st.get('bookings_today', 0)}, новых {st.get('created_today', 0)}"
                     f" · ошибок {st.get('errors', 0)}")
        if r.restarts:
            line += f"\n   перезапусков: {r.restarts}"
        parts.append(line)
    return "\n".join(parts)


async def supervise(ts: dict[str, dict], runners_out: dict | None = None):
    os.environ.pop("LUNKA_TENANT_DIR", None)
    os.environ.pop("LUNKA_TENANT", None)
    from app import adminbot, applog  # импорт после настройки окружения: менеджер — не салон

    runners = {slug: Runner(slug, cfg) for slug, cfg in ts.items()}
    if runners_out is not None:
        runners_out.update(runners)
    applog.STATUS_PROVIDER = lambda: status_text(runners)

    def log(text: str):
        print(re.sub(r"<[^>]+>", "", text), flush=True)
        applog.send_sync(text)

    print("\n" + "=" * 60)
    for slug, c in ts.items():
        print(f"  • {c.get('NAME', slug)}  [{slug}]  тип: {c.get('NICHE', 'nails')}")
    print("  Не закрывайте это окно — пока оно открыто, все салоны работают.")
    print("=" * 60 + "\n")
    log(f"🟢 Менеджер запущен, салонов: {len(runners)} ({', '.join(runners)})")
    try:
        await asyncio.gather(applog.run(listen=True), adminbot.run(), nightly_backups(log),
                             *(r.run(log) for r in runners.values()))
    finally:
        for r in runners.values():
            r.stop()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    TENANTS.mkdir(exist_ok=True)
    if cmd == "add":
        add_tenant_interactive()
        return
    if cmd == "max":
        connect_max_interactive()
        return
    if cmd == "backup":
        make_backup()
        return
    if cmd == "restore":
        if len(sys.argv) < 3:
            print("Укажите файл копии: python manager.py restore backups/backup_....zip")
            return
        restore_backup(sys.argv[2])
        return
    if cmd == "reset":
        reset_service()
        return
    if cmd == "rename":
        if len(sys.argv) >= 4:
            rename_tenant(sys.argv[2].lower(), sys.argv[3].lower())
        else:
            for slug, c in tenants().items():
                print(f"  {slug}  — {c.get('NAME', '')}")
            rename_tenant(ask("Какую папку переименовать: ").lower(), ask("Новое имя латиницей (например zarina): ").lower())
        return
    if cmd == "import":
        if len(sys.argv) < 4:
            print("Как: python manager.py import zarina salons/zarina.json")
            return
        import_salon(sys.argv[2].lower(), sys.argv[3])
        return
    if cmd == "list":
        for slug, c in tenants().items():
            print(f"{slug:15} {c.get('NICHE', 'nails'):6} порт {c.get('PORT')}  {c.get('NAME', '')}")
        return
    migrate_single_salon()
    if not tenants():
        print("Пока нет ни одного салона — давайте добавим первый.")
        add_tenant_interactive()
    ask_log_token()
    ask_admin_token()
    ts = tenants()
    errs = problems(ts)
    if errs:
        print("\nОШИБКА в настройках салонов:\n  " + "\n  ".join(errs))
        sys.exit(1)
    write_caddy(ts)
    try:
        asyncio.run(supervise(ts))
    except KeyboardInterrupt:
        print("Остановлено.")


if __name__ == "__main__":
    main()
