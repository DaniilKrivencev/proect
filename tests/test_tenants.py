"""Проверка, что несколько салонов на одном сервере не пересекаются: python -m tests.test_tenants"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import manager  # noqa: E402

tmp = Path(tempfile.mkdtemp())
manager.TENANTS = tmp / "tenants"
manager.SHARED = tmp / "shared"
manager.BASE = tmp
manager.TENANTS.mkdir()

# --- 1. Добавляем два салона разных типов ---
manager.write_tenant("zarina", {"NAME": "Ногти у Зарины", "NICHE": "nails", "BOT_TOKEN": "111:AAA", "PORT": 8101})
manager.write_tenant("masha", {"NAME": "Маша · волосы", "NICHE": "hair", "BOT_TOKEN": "222:BBB", "PORT": 8102})
ts = manager.tenants()
assert set(ts) == {"zarina", "masha"} and not manager.problems(ts)
assert manager.next_port(ts) == 8103

# одинаковый бот у двух салонов — запрещено
bad = dict(ts, copy={"BOT_TOKEN": "111:AAA", "PORT": "8109"})
assert any("ОДИН И ТОТ ЖЕ бот" in e for e in manager.problems(bad))

# --- 2. Каждый салон в своём процессе пишет в свою базу ---
WORKER = textwrap.dedent('''
    import os, json
    from fastapi.testclient import TestClient
    from app import api, db, config, niche
    db.init()
    H = {"X-Init-Data": "dev"}
    c = TestClient(api.app)
    me = c.get("/api/me", headers=H).json()
    c.post("/api/masters", headers=H, json={"name": os.environ["MASTER"]})
    services = [s["name"] for s in c.get("/api/services", headers=H).json()]
    print(json.dumps({"db": str(config.DB_PATH), "niche": me["niche"], "services": services,
                      "masters": [m["name"] for m in c.get("/api/masters", headers=H).json()]}, ensure_ascii=False))
''')
results = {}
for slug, master in (("zarina", "Аня"), ("masha", "Оля")):
    env = dict(os.environ, LUNKA_TENANT_DIR=str(manager.TENANTS / slug), LUNKA_TENANT=slug,
               DEV_USER_ID="1", PYTHONUTF8="1", MASTER=master)
    env.pop("DB_FILE", None)
    out = subprocess.run([sys.executable, "-c", WORKER], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    results[slug] = json.loads(out.stdout.strip().splitlines()[-1])

z, m = results["zarina"], results["masha"]
assert z["db"] != m["db"] and "zarina" in z["db"] and "masha" in m["db"], (z["db"], m["db"])
assert z["niche"] == "nails" and m["niche"] == "hair"
assert "Маникюр без покрытия" in z["services"] and "Женская стрижка" in m["services"]
assert "Женская стрижка" not in z["services"], "прайс парикмахерской попал в маникюр"
assert z["masters"] == ["Аня"] and m["masters"] == ["Оля"], "мастера смешались"
for slug, name in (("zarina", "Ногти у Зарины"), ("masha", "Маша · волосы")):
    con = sqlite3.connect(manager.TENANTS / slug / "lunka.db")
    assert con.execute("SELECT value FROM settings WHERE key='salon_name'").fetchone()[0] == name
print("✅ Базы раздельные:", Path(z["db"]).relative_to(tmp), "и", Path(m["db"]).relative_to(tmp))


# --- 3. Падение одного салона не трогает другой, упавший перезапускается ---
async def supervision():
    logs = []
    crash = manager.Runner("zarina", ts["zarina"], cmd=[sys.executable, "-c", "print('start'); import sys; sys.exit(3)"])
    alive = manager.Runner("masha", ts["masha"], cmd=[sys.executable, "-c", "import time; print('ok'); time.sleep(30)"])
    crash.BACKOFF = alive.BACKOFF = [0.2, 0.2, 0.2, 0.2]
    tasks = [asyncio.create_task(crash.run(logs.append)), asyncio.create_task(alive.run(logs.append))]
    await asyncio.sleep(6)
    assert alive.proc.returncode is None and alive.restarts == 0, "живой салон пострадал"
    assert crash.restarts >= 3, crash.restarts
    assert crash.state.startswith("остановлен"), crash.state  # 4 быстрых падения → перестаём и сообщаем
    assert any("[zarina]" in l and "перезапускаю" in l for l in logs)
    assert any("перестаю перезапускать" in l for l in logs)
    for r in (crash, alive):
        r.stop()
    for t in tasks:
        t.cancel()
    # у каждого процесса — только своя папка и никакого чужого токена
    e = alive.env()
    assert e["LUNKA_TENANT_DIR"].endswith("masha") and "BOT_TOKEN" not in e

asyncio.run(supervision())
print("✅ Падение одного салона не влияет на другой, перезапуск работает")

# --- 4. Перенос салона из старой версии (всё лежало в корне) ---
old = Path(tempfile.mkdtemp())
manager.BASE, manager.TENANTS, manager.SHARED = old, old / "tenants", old / "shared"
manager.TENANTS.mkdir()
con = sqlite3.connect(old / "lunka.db")
con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
con.executemany("INSERT INTO settings VALUES (?,?)", [("salon_name", "Лунка"), ("log_chat_id", "777")])
con.commit(); con.close()
(old / ".env").write_text("BOT_TOKEN=999:OLD\nLOG_BOT_TOKEN=5:LOG\n", encoding="utf-8")
manager.migrate_single_salon()
t = manager.tenants()
assert t["salon1"]["BOT_TOKEN"] == "999:OLD" and t["salon1"]["NAME"] == "Лунка"
assert (old / "tenants/salon1/lunka.db").exists() and not (old / "lunka.db").exists()
assert "BOT_TOKEN=999" not in (old / ".env").read_text(encoding="utf-8")
assert "LOG_BOT_TOKEN=5:LOG" in (old / ".env").read_text(encoding="utf-8")
assert json.loads((old / "shared/logbot.json").read_text())["chat_id"] == 777
print("✅ Старый салон перенесён в tenants/salon1 без потери данных и подключения логов")

shutil.rmtree(tmp, ignore_errors=True); shutil.rmtree(old, ignore_errors=True)
print("✅ Все проверки салонов пройдены")
