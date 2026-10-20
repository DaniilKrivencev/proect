"""Техподдержка: вход в салон как хозяин / мастер / клиент, телефоны скрыты. python -m tests.test_support"""
import os
import tempfile
from datetime import timedelta

os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="111", DB_FILE=os.path.join(tempfile.mkdtemp(), "t.db"), TUNNEL="off")
from fastapi.testclient import TestClient

from app import api, db

DEV = 999
db.init()
db.dev_id = lambda: DEV  # в жизни id берётся из shared/admin.json
c = TestClient(api.app)
OWNER = {"X-Init-Data": "dev"}           # настоящий владелец (DEV_USER_ID)
master = c.post("/api/masters", headers=OWNER, json={"name": "Аня"}).json()
svc = c.get("/api/services", headers=OWNER).json()[0]
anna = db.get_or_create_client(5001, "Анна", "+79001112233", "anna")
db.create_booking(master["id"], anna["id"], svc, (db.now_local() + timedelta(days=2)).strftime("%Y-%m-%d 12:00"))
SUP = {"X-Login": db.issue_login(DEV)}

# Без режима — техподдержка обычный человек, в кабинет не пускают
assert c.get("/api/me", headers=SUP).status_code == 403
assert not db.is_admin(DEV) and db.master_by_tg(DEV) is None

# Хозяин: всё видит, но телефоны скрыты
db.set_setting("dev_mode", "owner")
assert db.is_admin(DEV)
me = c.get("/api/me", headers=SUP).json()
assert me["role"] == "admin"
clients = c.get("/api/clients", headers=SUP).json()
assert clients[0]["phone"] == "+7 *** ***-**-33", clients[0]["phone"]
bookings = c.get("/api/bookings", headers=SUP).json()
assert all("2233" not in (b.get("client_phone") or "") for b in (bookings if isinstance(bookings, list) else bookings.get("items", [])))
assert "79001112233" not in c.get("/api/me", headers=SUP).text
# Сохранение карточки клиента не затирает настоящий телефон маской
assert c.put(f"/api/clients/{anna['id']}", headers=SUP, json={"note": "тест", "phone": "+7 *** ***-**-33"}).status_code == 200
assert db.one("SELECT phone FROM clients WHERE id=?", (anna["id"],))["phone"] == "+79001112233"

# Настоящий владелец видит полный номер
assert c.get("/api/clients", headers=OWNER).json()[0]["phone"] == "+79001112233"

# Мастер Аня: только её записи, телефоны скрыты
db.set_setting("dev_mode", f"master:{master['id']}")
assert not db.is_admin(DEV) and db.master_by_tg(DEV)["id"] == master["id"]
me = c.get("/api/me", headers=SUP).json()
assert me["role"] == "master" and "79001112233" not in c.get("/api/me", headers=SUP).text

# Клиент: в кабинет нельзя
db.set_setting("dev_mode", "client")
assert c.get("/api/me", headers=SUP).status_code == 403

# Обычного человека режим не касается
assert not db.is_admin(5001) and db.master_by_tg(5001) is None
print("✅ Техподдержка: роли переключаются, телефоны скрыты")
