"""Фото отзывов в копиях хранятся один раз: python -m tests.test_backup_photos"""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent


def project() -> Path:
    work = Path(tempfile.mkdtemp()) / "p"
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "tenants", "backups", "shared", ".venv"))
    t = work / "tenants" / "zarina"
    (t / "reviews").mkdir(parents=True)
    (t / "tenant.env").write_text("NAME=Z\nNICHE=nails\nBOT_TOKEN=1:a\nPORT=8101\n", encoding="utf-8")
    import sqlite3
    sqlite3.connect(t / "lunka.db").execute("CREATE TABLE x (a)").connection.commit()
    return work


def run(work: Path, code: str) -> str:
    return subprocess.run([sys.executable, "-c", "import manager as M\n" + code], cwd=work,
                          capture_output=True, text=True, check=True).stdout


work = project()
photos = work / "tenants" / "zarina" / "reviews"
(photos / ("a" * 24 + ".jpg")).write_bytes(b"\xff\xd8one")
(photos / ("b" * 24 + ".jpg")).write_bytes(b"\xff\xd8two")

# 1. Ночная: фото докопированы в хранилище, архив без фото
out = run(work, "print(M.sync_photos()); print(M.make_backup(True, None, False))").split()
assert out[0] == "2", out
nightly = Path(out[1])
assert not [n for n in zipfile.ZipFile(nightly).namelist() if n.endswith(".jpg")], "в ночной копии не должно быть фото"
store = work / "backups" / "photos" / "zarina"
assert sorted(f.name for f in store.iterdir()) == ["a" * 24 + ".jpg", "b" * 24 + ".jpg"]

# 2. Следующая ночь: копируются только новые
(photos / ("c" * 24 + ".jpg")).write_bytes(b"\xff\xd8three")
assert run(work, "print(M.sync_photos())").strip() == "1"
assert run(work, "print(M.sync_photos())").strip() == "0"

# 3. Ручная копия (для переезда) — полная, с фото
full = Path(run(work, "print(M.make_backup(True))").strip())
assert len([n for n in zipfile.ZipFile(full).namelist() if n.endswith(".jpg")]) == 3

# 4. Восстановление из ночной копии на этом же месте — фото возвращаются из хранилища
shutil.rmtree(work / "tenants")
run(work, f"M.ask = lambda *a: 'да'\nM.restore_backup({str(nightly)!r})")
assert sorted(f.name for f in photos.iterdir()) == ["a" * 24 + ".jpg", "b" * 24 + ".jpg", "c" * 24 + ".jpg"]

# 5. Переезд: полная копия на «чистое» место — фото внутри архива
new = project()
shutil.rmtree(new / "tenants")
run(new, f"M.restore_backup({str(full)!r})")
assert len(list((new / "tenants" / "zarina" / "reviews").glob("*.jpg"))) == 3
print("✅ Фото в копиях: ночные без дублей, ручная полная, восстановление возвращает все фото")
