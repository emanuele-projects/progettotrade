"""Supervisor: run the trading bot and the Streamlit dashboard side by side.

Exit-code contract (used by the Windows Task Scheduler auto-restart, and by
Railway if ever used):
- bot exits cleanly with rc 0  → deliberate shutdown (hard KILL_SWITCH) → exit 0,
  DO NOT restart.
- bot crashes (rc != 0) or the dashboard dies → exit 1 → supervisor restarts.

This is what makes "start on login + restart on failure" behave: a hard kill
stops for good, a crash comes back.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time


# Run from this file's directory regardless of where the scheduler launched us,
# so relative paths (main.py, dashboard.py, data/, .env) resolve.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

PORT = os.environ.get("PORT", "8501")

procs: list[subprocess.Popen] = []


def shutdown(*_):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


bot = subprocess.Popen([sys.executable, "-u", "main.py"])
dashboard = subprocess.Popen([
    sys.executable, "-m", "streamlit", "run", "dashboard.py",
    f"--server.port={PORT}",
    "--server.address=0.0.0.0",
    "--server.headless=true",
    "--browser.gatherUsageStats=false",
])
procs = [bot, dashboard]


def _terminate_siblings(exclude: subprocess.Popen) -> None:
    for q in procs:
        if q is not exclude and q.poll() is None:
            q.terminate()
    for q in procs:
        if q is not exclude:
            try:
                q.wait(timeout=10)
            except subprocess.TimeoutExpired:
                q.kill()


while True:
    for p in procs:
        rc = p.poll()
        if rc is not None:
            _terminate_siblings(p)
            # Deliberate shutdown: the bot exited 0 (hard KILL_SWITCH, or refused
            # to start because a hard kill file is present). Don't restart.
            if p is bot and rc == 0:
                sys.stderr.write("[runner] bot exited cleanly (hard kill) — not restarting\n")
                sys.exit(0)
            sys.stderr.write(f"[runner] pid={p.pid} exited rc={rc} — signaling restart\n")
            sys.exit(1)
    time.sleep(2)
