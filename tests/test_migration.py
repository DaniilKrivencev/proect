"""Переезд на другой сервер: копия → новый сервер → восстановление. python -m tests.test_migration"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
old, new = Path(tempfile.mkdtemp()) / "old", Path(tempfile.mkdtemp()) / "new"
shutil.copytree(ROOT, old, ignore=shutil.ignore_patterns("tenants", "shared", "backups", ".venv", "__pycache__", "*.db*", ".env"))
env = dict(os.environ, PYTHONUTF8="1")


def run(where, *args, inp=""):
    r = subprocess.run([sys.executable, "manager.py", *args], cwd=where, env=env, input=inp,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


# «Старый сервер»: два салона с данными, общий токен логов
sys.path.insert(0, str(old))
for slug, niche, tok in (("salon1", "nails", "111:AAA"), ("masha", "hair", "222:BBB")):
    d = old / "tenants" / slug
    d.mkdir(parents=True)
    (d / "tenant.env").write_text(f"NAME={slug}\nNICHE={niche}\nBOT_TOKEN={tok}\nPORT=81{len(slug)}\nMAX_BOT_TOKEN=\n", encoding="utf-8")
    con = sqlite3.connect(d / "lunka.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO clients (name) VALUES (?)", [(f"{slug}-клиент-{i}",) for i in range(50)])
    con.commit()
    con.close()
(old / ".env").write_text("LOG_BOT_TOKEN=5:LOG\n", encoding="utf-8")
(old / "shared").mkdir()
(old / "shared" / "logbot.json").write_text('{"chat_id": 777}', encoding="utf-8")

# Салон «работает»: база открыта, в неё пишут, а изменения ещё в журнале WAL
live = sqlite3.connect(old / "tenants/salon1/lunka.db")
live.execute("PRAGMA journal_mode=WAL")
live.execute("INSERT INTO clients (name) VALUES ('свежая запись прямо перед копией')")
live.commit()

out = run(old, "backup")
zips = list((old / "backups").glob("backup_*.zip"))
assert len(zips) == 1 and "Копия готова" in out, out
live.close()

# 2. Переименование папки салона
print(run(old, "rename", "salon1", "zarina").strip())
assert (old / "tenants/zarina/lunka.db").exists() and not (old / "tenants/salon1").exists()

# 3. «Новый сервер»: только manager.py и файл копии
new.mkdir(parents=True)
shutil.copy(old / "manager.py", new / "manager.py")
shutil.copy(zips[0], new / "copy.zip")
print(run(new, "restore", "copy.zip").strip().splitlines()[-1])

for slug in ("salon1", "masha"):
    con = sqlite3.connect(new / "tenants" / slug / "lunka.db")
    n = con.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    con.close()
    assert n == (51 if slug == "salon1" else 50), (slug, n)
assert "BOT_TOKEN=111:AAA" in (new / "tenants/salon1/tenant.env").read_text(encoding="utf-8")
assert "LOG_BOT_TOKEN=5:LOG" in (new / ".env").read_text(encoding="utf-8")
assert (new / "shared/logbot.json").exists()
for f in ("app/bot.py", "app/maxbot.py", "webapp/app.js", "requirements.txt", "app/certs/russian_trusted_ca.pem"):
    assert (new / f).exists(), f"в копии нет {f}"
listing = run(new, "list")
assert "salon1" in listing and "masha" in listing
print("✅ Переезд: все салоны, клиенты (включая свежую запись), токены и код на новом месте")

# 4. Восстановление поверх существующих салонов — с подтверждением, старое сохраняется
print(run(new, "restore", "copy.zip", inp="да\n").strip().splitlines()[-1])
assert list(new.glob("tenants_old_*")), "старые данные не сохранены"
print("✅ Все проверки переезда пройдены")
