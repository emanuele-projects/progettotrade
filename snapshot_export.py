"""Publish a compact journal snapshot for the public Vercel dashboard.

The journal (Claude's market views, per-symbol reasoning, distilled lessons,
equity curve) lives in SQLite on the Oracle VM — Vercel can't see it. This
module builds a small JSON snapshot and uploads it to Vercel Blob storage over
plain HTTPS (stdlib only). The Vercel function fetches it via SNAPSHOT_URL and
serves it behind the dashboard password.

Setup (one time):
  1. Vercel dashboard → Storage → Create → Blob.
  2. Copy the BLOB_READ_WRITE_TOKEN into the VM's .env.
  3. After the first upload the bot logs the public URL → set it as
     SNAPSHOT_URL in the Vercel project env vars.

Without the token the exporter still writes data/snapshot.json locally and
skips the upload silently — the bot never depends on it.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from config import CFG
import journal


def build_snapshot() -> dict:
    """Assemble the dashboard-facing view of the journal. Read-only, cheap."""
    decisions = []
    for d in journal.recent_decisions(limit=12):
        try:
            items = json.loads(d.get("decisions_json") or "[]")
        except Exception:
            items = []
        decisions.append({
            "ts": d.get("ts"),
            "trigger": d.get("trigger"),
            "market_view": (d.get("market_view") or "")[:600],
            "decisions": [{
                "symbol": i.get("symbol", ""),
                "action": i.get("action", ""),
                "confidence": i.get("confidence"),
                "leverage": i.get("leverage"),
                "sl": i.get("stop_loss_pct"),
                "tp": i.get("take_profit_pct"),
                "reasoning": str(i.get("reasoning", ""))[:400],
            } for i in items if isinstance(i, dict)],
        })

    curve = journal.equity_curve()
    if len(curve) > 300:  # downsample: the dashboard chart needs shape, not detail
        step = len(curve) / 300
        curve = [curve[int(i * step)] for i in range(300)]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decisions": decisions,
        "lessons": journal.get_active_lessons(limit=CFG.MEMORY_MAX_LESSONS),
        "equity_curve": curve,
        "last_reflection": journal.get_meta("last_reflection_ts"),
        "initial_capital": CFG.INITIAL_CAPITAL_USDT,
    }


def export(log=None) -> str | None:
    """Write the local snapshot and upload to Vercel Blob if a token is set.
    Returns the public URL (or None). Never raises."""
    try:
        body = json.dumps(build_snapshot(), ensure_ascii=False).encode()
        (CFG.DATA_DIR / "snapshot.json").write_bytes(body)  # local copy, always

        token = os.getenv("BLOB_READ_WRITE_TOKEN", "")
        if not token:
            return None
        req = urllib.request.Request(
            "https://blob.vercel-storage.com/" + CFG.SNAPSHOT_BLOB_PATH,
            data=body, method="PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "x-api-version": "7",
                "x-add-random-suffix": "0",       # stable URL across uploads
                "x-cache-control-max-age": "60",  # CDN staleness ≤ 1 min
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode())
        url = resp.get("url")
        if url and journal.get_meta("snapshot_url") != url:
            journal.set_meta("snapshot_url", url)
            journal.log_event("SNAPSHOT", f"public snapshot URL: {url}")
            if log is not None:
                log.info(f"snapshot uploaded — set SNAPSHOT_URL on Vercel to: {url}")
        return url
    except Exception as e:
        if log is not None:
            log.warning(f"snapshot export failed: {e}")
        return None
