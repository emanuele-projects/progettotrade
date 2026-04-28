"""Supervisor: run the trading bot and the Streamlit dashboard side by side.

If either subprocess dies, terminate the other and exit non-zero so the
Railway restart policy brings both back up together.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time


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


procs.append(subprocess.Popen([sys.executable, "-u", "main.py"]))
procs.append(subprocess.Popen([
    sys.executable, "-m", "streamlit", "run", "dashboard.py",
    f"--server.port={PORT}",
    "--server.address=0.0.0.0",
    "--server.headless=true",
    "--browser.gatherUsageStats=false",
]))


while True:
    for p in procs:
        rc = p.poll()
        if rc is not None:
            sys.stderr.write(f"[runner] subprocess pid={p.pid} exited rc={rc}; terminating siblings\n")
            for q in procs:
                if q is not p and q.poll() is None:
                    q.terminate()
            sys.exit(rc or 1)
    time.sleep(2)
