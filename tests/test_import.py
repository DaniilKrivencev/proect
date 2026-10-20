"""Заполнение салона из файла: python -m tests.test_import"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
work = Path(tempfile.mkdtemp()) / "p"
shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "tenants", "backups", "shared"))
t = work / "tenants" / "zarina"
t.mkdir(parents=True)
(t / "tenant.env").write_text("NAME=Ногти у Зарины\nNICHE=nails\nBOT_TOKEN=1:a\nPORT=8101\n", encoding="utf-8")

env = {**os.environ, "LUNKA_TENANT_DIR": str(t), "LUNKA_TENANT": "zarina"}
run = lambda code: subprocess.run([sys.executable, "-c", code], cwd=work, env=env, capture_output=True, text=True, check=True).stdout

# Салон уже работал: стартовый прайс, тестовый мастер и запись на старую услугу
run("""
from app import db; db.init()
m = db.create_master('Тест')
s = db.q('SELECT * FROM services')[0]
c = db.get_or_create_client(1, 'Клиент', '+7900')
db.create_booking(m['id'], c['id'], s, '2026-12-01 12:00')
""")

for _ in range(2):  # повторный запуск ничего не дублирует
    out = subprocess.run([sys.executable, "manager.py", "import", "zarina", "salons/zarina.json"], cwd=work,
                         capture_output=True, text=True, check=True).stdout
    assert "✅" in out, out

data = json.loads((work / "salons/zarina.json").read_text(encoding="utf-8"))
res = json.loads(run("""
import json
from app import db; db.init()
out = {
 'name': db.settings()['salon_name'],
 'active': db.one('SELECT COUNT(*) n FROM services WHERE active=1')['n'],
 'bookable': len(db.bookable_services()),
 'bookings': db.one('SELECT COUNT(*) n FROM bookings')['n'],
 'masters': {m['name']: [s['name'] for s in db.q('SELECT s.name FROM services s JOIN master_services ms ON ms.service_id=s.id WHERE ms.master_id=? AND s.active=1', (m['id'],))] for m in db.masters()},
 'for_botox': [m['name'] for m in db.masters_for_service(db.one("SELECT id FROM services WHERE name LIKE 'Ботулино%'")['id'])],
 'for_japan': [m['name'] for m in db.masters_for_service(db.one("SELECT id FROM services WHERE name='Японский маникюр'")['id'])],
}
print(json.dumps(out, ensure_ascii=False))
""").strip().splitlines()[-1])

assert res["name"] == "Стиль и Я"
assert res["active"] == len(data["services"]) == res["bookable"], res
assert res["bookings"] == 1, "старая запись пропала"
assert res["for_botox"] == ["Косметолог Кристина"], res["for_botox"]
assert set(res["for_japan"]) == {"Nasirova Burulai", "Жанна", "Миронова Мария"}, res["for_japan"]
assert "Френч" in res["masters"]["Жанна"] and "Френч" not in res["masters"]["Nasirova Burulai"]
assert "Ремонт ногтя" in res["masters"]["Миронова Мария"]
assert "NAME=Стиль и Я" in (t / "tenant.env").read_text(encoding="utf-8")
for name, svcs in res["masters"].items():
    if name != "Тест":
        print(f"  {name}: {len(svcs)} услуг")
print("✅ Салон заполняется из файла, повторный запуск ничего не ломает")
