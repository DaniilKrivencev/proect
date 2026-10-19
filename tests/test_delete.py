"""Удаление клиентов и переписки: python -m tests.test_delete"""
import os
import tempfile
from datetime import timedelta

os.environ.update(BOT_TOKEN="123:TEST", DEV_USER_ID="111", DB_FILE=os.path.join(tempfile.mkdtemp(), "t.db"), TUNNEL="off")
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import api, db

db.init()
c = TestClient(api.app)
H = {"X-Init-Data": "dev"}
master = c.post("/api/masters", headers=H, json={"name": "Аня"}).json()
svc = c.get("/api/services", headers=H).json()[0]

anna = db.get_or_create_client(5001, "Анна", "+79001112233", "anna")
db.log_message(anna["id"], "in", "Здравствуйте")
db.log_message(anna["id"], "out", "Добрый день!", author="Владелец")
past = (db.now_local() - timedelta(days=3)).strftime("%Y-%m-%d 12:00")
future = (db.now_local() + timedelta(days=3)).strftime("%Y-%m-%d 12:00")
db.create_booking(master["id"], anna["id"], svc, past)
fut_id = db.create_booking(master["id"], anna["id"], svc, future)

# 1. Удаление переписки: сообщений нет, клиент и записи остались
assert c.get("/api/chats", headers=H).json()
assert c.delete(f"/api/chats/{anna['id']}", headers=H).status_code == 200
assert c.get("/api/chats", headers=H).json() == []
assert any(x["id"] == anna["id"] for x in c.get("/api/clients", headers=H).json())

# 2. Мастер удалять не может
api_parse = api.safe_parse_webapp_init_data
db.ex("UPDATE masters SET tg_id=777 WHERE id=?", (master["id"],))
api.safe_parse_webapp_init_data = lambda token, init_data: SimpleNamespace(user=SimpleNamespace(id=777, first_name="Аня", last_name=None))
assert c.delete(f"/api/clients/{anna['id']}", headers={"X-Init-Data": "x"}).status_code == 403
assert c.delete(f"/api/chats/{anna['id']}", headers={"X-Init-Data": "x"}).status_code == 403
api.safe_parse_webapp_init_data = api_parse

# 3. С будущей записью — нельзя; после отмены — можно
r = c.delete(f"/api/clients/{anna['id']}", headers=H)
assert r.status_code == 409 and "предстоящих" in r.json()["detail"]
db.cancel_booking(fut_id, "salon")
db.log_message(anna["id"], "in", "ещё сообщение")
assert c.delete(f"/api/clients/{anna['id']}", headers=H).status_code == 200

row = db.one("SELECT * FROM clients WHERE id=?", (anna["id"],))
assert row["name"] == "Удалённый клиент" and row["phone"] == "" and row["tg_id"] is None and row["deleted"] == 1
assert not db.q("SELECT 1 FROM messages WHERE client_id=?", (anna["id"],)), "переписка не стёрта"
assert all(x["id"] != anna["id"] for x in c.get("/api/clients", headers=H).json()), "удалённый клиент в списке"
assert c.get("/api/me", headers=H).json()["stats"]["month_revenue"] >= 0
assert db.one("SELECT COUNT(*) n FROM bookings WHERE client_id=?", (anna["id"],))["n"] == 2, "история визитов пропала"

# 4. Если та же клиентка напишет снова — она будет новым клиентом, без старых данных
again = db.get_or_create_client(5001, "Анна", "", "anna")
assert again["id"] != anna["id"]
print("✅ Удаление клиентов и переписки работает")
