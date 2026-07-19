"""SQLite journal: every decision, trade, equity snapshot, and event lands here."""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from config import CFG


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_view TEXT,
    decisions_json TEXT NOT NULL,
    raw_response TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    notional_usdt REAL NOT NULL,
    leverage INTEGER NOT NULL,
    kind TEXT NOT NULL,
    note TEXT,
    sl_pct REAL,    -- Claude-decided stop-loss % on collateral, only on kind='open'
    tp_pct REAL     -- Claude-decided take-profit % on collateral, only on kind='open'
);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    wallet_balance REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    total_equity REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    note TEXT NOT NULL,
    symbol TEXT,            -- optional symbol the note targets (NULL = applies to all)
    expires_at TEXT,        -- optional ISO timestamp; NULL = no expiry
    active INTEGER NOT NULL DEFAULT 1
);

-- Long-term memory: durable, self-authored lessons distilled by the periodic
-- reflection loop (memory.reflect). One row per lesson; the active set is what
-- gets injected into future decision prompts. A reflection deactivates the old
-- set and inserts a fresh one (carry-forward is decided by the model).
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',  -- 'global' or a SYMBOL (e.g. DOGEUSDT)
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

-- Small key/value store for bot-wide state that must survive restarts
-- (e.g. the timestamp of the last reflection run).
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    # timeout + busy_timeout: multiple writers (main loop, risk engine thread,
    # dashboard process) contend on this file — wait instead of raising
    # "database is locked".
    conn = sqlite3.connect(CFG.JOURNAL_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    CFG.JOURNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        # WAL lets readers (dashboard) coexist with concurrent writer threads.
        # It's a property of the db file itself — set once, applies to every
        # connection (including the raw ones in execution.py / shadow.py).
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # Lightweight migration: SQLite throws if the column already exists;
        # swallow and continue.
        migrations = [
            ("trades", "sl_pct", "REAL"),
            ("trades", "tp_pct", "REAL"),
            ("trades", "trigger", "TEXT"),       # cycle | event:price_move | risk:sl | risk:liq_guard | ...
            ("decisions", "trigger", "TEXT"),
            ("decisions", "model", "TEXT"),
            ("decisions", "input_tokens", "INTEGER"),
            ("decisions", "output_tokens", "INTEGER"),
        ]
        for table, col, coltype in migrations:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass


def log_decision(market_view: str, decisions: list[dict], raw: str | None = None,
                 trigger: str | None = None, model: str | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO decisions (ts, market_view, decisions_json, raw_response, "
            "trigger, model, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), market_view, json.dumps(decisions), raw,
             trigger, model, input_tokens, output_tokens),
        )


def log_trade(symbol: str, side: str, qty: float, price: float, notional: float,
              leverage: int, kind: str, note: str = "",
              sl_pct: float | None = None, tp_pct: float | None = None,
              trigger: str | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, notional_usdt, leverage, kind, note, sl_pct, tp_pct, trigger) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, side, qty, price, notional, leverage, kind, note, sl_pct, tp_pct, trigger),
        )


def get_position_targets(symbol: str) -> tuple[float, float]:
    """Return (sl_pct, tp_pct) Claude set for the latest open of `symbol`.

    Falls back to global CFG defaults when no custom targets were stored
    (legacy positions opened before this feature shipped, or rows where
    Claude omitted them)."""
    with db() as c:
        row = c.execute(
            "SELECT sl_pct, tp_pct FROM trades "
            "WHERE symbol=? AND kind='open' ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        sl = row["sl_pct"] if (row and row["sl_pct"] is not None) else CFG.HARD_STOP_LOSS_PCT
        tp = row["tp_pct"] if (row and row["tp_pct"] is not None) else CFG.TAKE_PROFIT_PCT
        return float(sl), float(tp)


def log_equity(wallet: float, unrealized: float, equity: float,
               open_positions: int, source: str = "live") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO equity (ts, wallet_balance, unrealized_pnl, total_equity, open_positions, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), wallet, unrealized, equity, open_positions, source),
        )


def log_event(level: str, msg: str) -> None:
    with db() as c:
        c.execute("INSERT INTO events (ts, level, msg) VALUES (?, ?, ?)", (_now(), level, msg))


def latest_equity(source: str = "live") -> float | None:
    with db() as c:
        row = c.execute(
            "SELECT total_equity FROM equity WHERE source = ? ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        return row["total_equity"] if row else None


def equity_curve(source: str = "live") -> list[tuple[str, float]]:
    with db() as c:
        rows = c.execute(
            "SELECT ts, total_equity FROM equity WHERE source = ? ORDER BY id ASC",
            (source,),
        ).fetchall()
        return [(r["ts"], r["total_equity"]) for r in rows]


# ---------------------------------------------------------------------------
# Operator notes — manual context the operator surfaces to Claude.
# ---------------------------------------------------------------------------
def add_operator_note(note: str, symbol: str | None = None,
                      expires_hours: float | None = 48) -> int:
    """Insert an operator note. Returns the row id.

    Notes default to expiring in 48h. Pass expires_hours=None to keep it
    indefinitely (until manually deactivated)."""
    expires_at = None
    if expires_hours is not None:
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(hours=expires_hours)).isoformat()
    with db() as c:
        cur = c.execute(
            "INSERT INTO operator_notes (ts, note, symbol, expires_at, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (_now(), note.strip(), symbol, expires_at),
        )
        return int(cur.lastrowid)


def get_active_operator_notes(symbol: str | None = None) -> list[dict]:
    """Active = active=1 AND (expires_at is NULL OR expires_at > now).
    If `symbol` is provided, returns notes targeting that symbol OR global notes.
    Otherwise returns all active notes."""
    now_iso = _now()
    with db() as c:
        if symbol is None:
            rows = c.execute(
                "SELECT id, ts, note, symbol, expires_at FROM operator_notes "
                "WHERE active=1 AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY id DESC",
                (now_iso,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, ts, note, symbol, expires_at FROM operator_notes "
                "WHERE active=1 AND (expires_at IS NULL OR expires_at > ?) "
                "AND (symbol = ? OR symbol IS NULL) "
                "ORDER BY id DESC",
                (now_iso, symbol),
            ).fetchall()
        return [dict(r) for r in rows]


def deactivate_operator_note(note_id: int) -> None:
    with db() as c:
        c.execute("UPDATE operator_notes SET active=0 WHERE id=?", (note_id,))


# ---------------------------------------------------------------------------
# Long-term memory — self-authored lessons + a tiny key/value store.
# ---------------------------------------------------------------------------
def replace_lessons(lessons: list[dict]) -> int:
    """Atomically supersede the active lesson set with a fresh one.

    Each item is {"scope": str, "text": str}. The old active set is deactivated
    (kept for history, not deleted) and the new set inserted active. Returns the
    number of lessons stored. Empty input is a no-op (keeps the current set) so a
    failed/empty reflection never wipes the bot's memory."""
    cleaned = [
        {"scope": (l.get("scope") or "global").strip() or "global",
         "text": str(l.get("text", "")).strip()[:280]}
        for l in lessons if str(l.get("text", "")).strip()
    ]
    if not cleaned:
        return 0
    with db() as c:
        c.execute("UPDATE lessons SET active=0 WHERE active=1")
        c.executemany(
            "INSERT INTO lessons (ts, scope, text, active) VALUES (?, ?, ?, 1)",
            [(_now(), l["scope"], l["text"]) for l in cleaned],
        )
    return len(cleaned)


def add_lesson(text: str, scope: str = "global") -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO lessons (ts, scope, text, active) VALUES (?, ?, ?, 1)",
            (_now(), (scope or "global").strip() or "global", text.strip()[:280]),
        )
        return int(cur.lastrowid)


def get_active_lessons(limit: int = 12) -> list[dict]:
    """Active lessons, newest first (the current memory the bot reads).

    Tolerant of a not-yet-migrated DB (e.g. a dashboard opened before the bot
    has run init() to create the table) — returns [] instead of raising."""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT id, ts, scope, text FROM lessons WHERE active=1 "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def deactivate_lesson(lesson_id: int) -> None:
    with db() as c:
        c.execute("UPDATE lessons SET active=0 WHERE id=?", (lesson_id,))


def set_meta(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO bot_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), _now()),
        )


def get_meta(key: str, default: str | None = None) -> str | None:
    try:
        with db() as c:
            row = c.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
    except sqlite3.OperationalError:
        return default


def last_losing_exit_ts(symbol: str) -> str | None:
    """ISO ts of the most recent LOSING protective exit (stop-loss or
    liquidation-guard) on `symbol`, or None. Take-profits don't count — a
    winner re-qualifying is fine; a loser re-entered immediately is churn."""
    with db() as c:
        row = c.execute(
            "SELECT MAX(ts) AS ts FROM trades WHERE symbol=? AND kind IN ('sl','liq_guard')",
            (symbol,),
        ).fetchone()
        return row["ts"] if row and row["ts"] else None


def count_opens_since(since_iso: str, symbol: str | None = None) -> int:
    """Number of 'open' trades since `since_iso` (optionally for one symbol)."""
    with db() as c:
        if symbol is None:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE kind='open' AND ts >= ?",
                (since_iso,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE kind='open' AND ts >= ? AND symbol=?",
                (since_iso, symbol),
            ).fetchone()
        return int(row["n"]) if row else 0


def recent_decisions(limit: int = 15) -> list[dict]:
    """Latest decisions (market_view + per-symbol reasoning) for the reflection
    loop to read back its own recent thinking."""
    with db() as c:
        rows = c.execute(
            "SELECT ts, market_view, decisions_json, trigger FROM decisions "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
