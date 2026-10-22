"""Development mode: the app server runs as a child process and is restarted whenever the Python code
changes. The page reloads itself when the server restarts or a web file changes (it polls /api/dev).

A running scan, export or analysis is never cut short: the restart waits until it has finished.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from photoorg.server import PORT_TRIES, _free, _ours, open_window

PACKAGE = Path(__file__).parent
POLL_SECONDS = 1.0
SETTLE_SECONDS = 0.4  # editors often write a file in several steps


def snapshot() -> dict[Path, float]:
    """Modification times of the Python sources (web files need no restart: the page reloads itself)."""
    return {p: p.stat().st_mtime for p in PACKAGE.rglob("*.py")}


def _job_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/job", timeout=2) as r:
            return bool(json.load(r).get("running"))
    except (OSError, ValueError):
        return False


def _shutdown(port: int) -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST"), timeout=3)
    except OSError:
        pass  # already gone


def _wait(condition, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.2)
    return False


def run_dev(work: Path, income: Path, tz: str, port: int, window: bool = True) -> None:
    port = next((p for p in range(port, port + PORT_TRIES) if _free(p) or _ours(p)), port)
    work.mkdir(parents=True, exist_ok=True)
    (work / "app.port").write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    cmd = [sys.executable, "-m", "photoorg", "--income", str(income), "--work", str(work), "--tz", tz,
           "app", "--no-window", "--port", str(port), "--strict-port"]
    env = {**os.environ, "PHOTOORG_DEV": "1"}

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    def start() -> subprocess.Popen | None:
        if not _wait(lambda: _free(port), 15):  # the previous server must have released the port
            log(f"la porta {port} è ancora occupata da un'altra istanza dell'app: chiudila, poi salva un file per riprovare")
            return None
        child = subprocess.Popen(cmd, env=env)
        if _wait(lambda: _ours(port) or child.poll() is not None, 60) and child.poll() is None:
            log(f"server pronto su {url}")
        else:
            log("il server non è partito: correggi l'errore, riparte al prossimo salvataggio")
        return child

    def stop(child: subprocess.Popen | None) -> None:
        if child is not None and child.poll() is None:
            _shutdown(port)  # graceful first: open SQLite transactions end cleanly
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()

    if _ours(port):
        log(f"c'è già l'app su {url}: la sostituisco con il server di sviluppo")
        _wait(lambda: not _job_running(port), 24 * 3600)
        _shutdown(port)
    child = start()
    if window:
        open_window(url)
    last = snapshot()
    try:
        while True:
            time.sleep(POLL_SECONDS)
            if snapshot() == last:
                continue
            time.sleep(SETTLE_SECONDS)
            current = snapshot()
            changed = sorted(p.relative_to(PACKAGE).as_posix() for p in set(last) | set(current)
                             if last.get(p) != current.get(p))
            last = current
            if _job_running(port):
                log("modifiche al codice: aspetto la fine dell'operazione in corso")
                _wait(lambda: not _job_running(port), 24 * 3600)
            log(f"riavvio ({', '.join(changed[:4]) or 'codice cambiato'})")
            stop(child)
            child = start()
    except KeyboardInterrupt:
        log("chiusura")
    finally:
        stop(child)
