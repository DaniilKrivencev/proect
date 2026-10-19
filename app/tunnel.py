"""Автоматический https-адрес для кабинета через бесплатный туннель Cloudflare.

Telegram открывает Mini App только по https. Пока сервис работает на вашем компьютере,
cloudflared даёт временный адрес вида https://xxx.trycloudflare.com. Он меняется при каждом
запуске — бот сам обновляет кнопку «Кабинет» у всех мастеров.

Протокол http2 вместо стандартного QUIC — так туннель надёжнее работает в России.
Если туннель упал, main.py перезапускает его и раздаёт новый адрес.
"""
import atexit
import logging
import re
import shutil
import subprocess
import threading

from .config import BASE_DIR, TENANT_DIR

log = logging.getLogger("tunnel")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
LOG_FILE = TENANT_DIR / "tunnel.log"

_proc: subprocess.Popen | None = None
_state = {"connected": False}


def find_cloudflared() -> str | None:
    for name in ("cloudflared.exe", "cloudflared"):
        p = BASE_DIR / name
        if p.exists():
            return str(p)
    return shutil.which("cloudflared")


def alive() -> bool:
    return _proc is not None and _proc.poll() is None and _state["connected"]


def stop():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(5)
        except Exception:
            _proc.kill()
    _proc = None


atexit.register(stop)


def start(port: int, timeout: float = 60) -> str | None:
    """Запустить туннель и дождаться, пока он реально подключится к Cloudflare."""
    global _proc
    stop()
    exe = find_cloudflared()
    if not exe:
        log.warning("cloudflared не найден — кабинет мастера будет недоступен (запись клиентов работает).")
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=flags)
    found: dict = {}
    _state["connected"] = False
    ready = threading.Event()
    proc = _proc

    def reader():
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                f.flush()
                m = URL_RE.search(line)
                if m and "url" not in found:
                    found["url"] = m.group(0)
                if "Registered tunnel connection" in line:
                    _state["connected"] = True
                    ready.set()
        _state["connected"] = False
        ready.set()  # процесс завершился

    threading.Thread(target=reader, daemon=True).start()
    ready.wait(timeout)
    url = found.get("url")
    if url and _state["connected"]:
        log.info("Кабинет доступен по адресу %s", url)
        return url
    log.warning("Туннель не смог подключиться за %s сек. Подробности в файле tunnel.log. "
                "Если у вас включён VPN — попробуйте выключить его (или наоборот включить) и перезапустить.",
                int(timeout))
    stop()
    return None
