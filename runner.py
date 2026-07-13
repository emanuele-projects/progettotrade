"""Persistent supervisor: run the trading bot + Streamlit dashboard, restart
either one if it crashes, and stop cleanly on a hard KILL_SWITCH.

Design (learned the hard way): the restart loop lives HERE, not in a .bat wrapper.
A cmd loop spawns orphan cmd processes that keep relaunching after you kill the
python — this supervisor owns its children and is the single restart authority.

- bot exits 0        → deliberate shutdown (hard KILL_SWITCH) → stop the dashboard, exit 0
- bot exits non-zero → crash → relaunch it after a short backoff
- dashboard dies     → relaunch it (non-critical, always)
- single-instance lock (loopback port) → a second supervisor exits immediately
"""
from __future__ import annotations
import os
import signal
import socket
import subprocess
import sys
import time


os.chdir(os.path.dirname(os.path.abspath(__file__)))

# --- single-instance guard -------------------------------------------------
_LOCK_PORT = 47821
_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _lock.bind(("127.0.0.1", _LOCK_PORT))
    _lock.listen(1)
except OSError:
    sys.stderr.write("[runner] another supervisor is already running — exiting\n")
    sys.exit(0)

PORT = os.environ.get("PORT", "8501")
_children: list[subprocess.Popen] = []


def _spawn_bot() -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-u", "main.py"])
    _children.append(p)
    return p


def _spawn_dashboard() -> subprocess.Popen:
    p = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", "dashboard.py",
        f"--server.port={PORT}",
        "--server.address=0.0.0.0",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ])
    _children.append(p)
    return p


def _shutdown(*_):
    for p in _children:
        if p.poll() is None:
            p.terminate()
    for p in _children:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def main() -> None:
    bot = _spawn_bot()
    dashboard = _spawn_dashboard()

    while True:
        rc = bot.poll()
        if rc is not None:
            if rc == 0:
                sys.stderr.write("[runner] bot exited cleanly (hard kill) — shutting down\n")
                if dashboard.poll() is None:
                    dashboard.terminate()
                sys.exit(0)
            sys.stderr.write(f"[runner] bot crashed rc={rc} — restarting in 10s\n")
            time.sleep(10)
            bot = _spawn_bot()

        if dashboard.poll() is not None:
            sys.stderr.write("[runner] dashboard exited — restarting in 5s\n")
            time.sleep(5)
            dashboard = _spawn_dashboard()

        time.sleep(2)


if __name__ == "__main__":
    main()
