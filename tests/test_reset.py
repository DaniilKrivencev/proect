"""Очистка сервиса перед выкладкой: python -m tests.test_reset"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
work = Path(tempfile.mkdtemp())
proj = work / "project"
shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("tenants", "shared", "backups", ".venv", "__pycache__", "*.db*", ".env"))
env = dict(os.environ, PYTHONUTF8="1")
env.pop("DB_FILE", None)


def manager(*args, inp=""):
    r = subprocess.run([sys.executable, "manager.py", *args], cwd=proj, env=env, input=inp, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def make_salon(slug, niche, token):
    d = proj / "tenants" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "tenant.env").write_text(f"NAME=Салон {slug}\nNICHE={niche}\nBOT_TOKEN={token}\nPORT=81{len(slug)}\nMAX_BOT_TOKEN=\n", encoding="utf-8")
    # заполняем базу «тестовыми» данными через настоящий код
    code = ("from app import db; db.init(); db.add_admin(111,'Я'); m=db.create_master('Тест-мастер');"
            "c=db.get_or_create_client(5,'Тестовая клиентка','+79990000000'); db.log_message(c['id'],'in','тест');"
            "db.create_booking(m['id'],c['id'],db.services()[0],'2026-10-25 12:00')")
    subprocess.run([sys.executable, "-c", code], cwd=proj, check=True,
                   env=dict(env, LUNKA_TENANT_DIR=str(d), LUNKA_TENANT=slug))
    (d / "status.json").write_text("{}", encoding="utf-8")
    (d / "tunnel.log").write_text("log", encoding="utf-8")


def counts(slug):
    code = ("from app import db; db.init(); import json;"
            "print(json.dumps([len(db.admins()), len(db.masters()), db.one('SELECT COUNT(*) n FROM clients')['n'],"
            "db.one('SELECT COUNT(*) n FROM bookings')['n'], len(db.services()), db.settings()['salon_name']], ensure_ascii=False))")
    d = proj / "tenants" / slug
    out = subprocess.run([sys.executable, "-c", code], cwd=proj, capture_output=True, text=True, check=True,
                         env=dict(env, LUNKA_TENANT_DIR=str(d), LUNKA_TENANT=slug)).stdout
    return eval(out.strip().splitlines()[-1])


make_salon("zarina", "nails", "111:AAA")
make_salon("masha", "hair", "222:BBB")
(proj / "shared").mkdir(exist_ok=True)
(proj / "shared/logbot.json").write_text('{"chat_id": 1}', encoding="utf-8")
manager("backup")
assert counts("zarina")[:4] == [1, 1, 1, 1]

# 0. Без слова ОЧИСТИТЬ ничего не удаляется
out = manager("reset", inp="1\n\nнет\n")
assert "ничего не удалено" in out and counts("zarina")[:4] == [1, 1, 1, 1]

# 1. Очистить данные, оставить ботов
out = manager("reset", inp="1\n\nОЧИСТИТЬ\nда\n")
assert "Сервис чистый" in out, out
for slug in ("zarina", "masha"):
    assert (proj / "tenants" / slug / "tenant.env").exists(), "токены бота пропали"
    assert not (proj / "tenants" / slug / "status.json").exists()
admins, masters, clients, bookings, services, name = counts("zarina")
assert (admins, masters, clients, bookings) == (0, 0, 0, 0), "данные не очистились"
assert services == 7 and name == "Салон zarina", (services, name)  # стартовый прайс и название вернулись
assert counts("masha")[4] == 8                                     # у парикмахерской — свой прайс
assert not (proj / "backups").exists(), "старые копии с тестовыми данными остались"
safe = list(work.glob("lunka_копия_до_очистки_*.zip"))
assert safe, "нет страховочной копии вне проекта"
assert "BOT_TOKEN=111:AAA" in (proj / "tenants/zarina/tenant.env").read_text(encoding="utf-8")
print("✅ Вариант 1: данные стёрты, боты остались, страховочная копия вне проекта:", safe[0].name)

# 2. Полный сброс одного салона, потом всех
make_salon("zarina", "nails", "111:AAA")
out = manager("reset", inp="2\nmasha\nОЧИСТИТЬ\n")
assert not (proj / "tenants/masha").exists() and (proj / "tenants/zarina").exists()
out = manager("reset", inp="2\n\nОЧИСТИТЬ\n")
assert not any((proj / "tenants").iterdir()) and not (proj / "shared").exists()
print("✅ Вариант 2: салоны удалены, сервис пустой")
shutil.rmtree(work, ignore_errors=True)
print("✅ Все проверки очистки пройдены")
