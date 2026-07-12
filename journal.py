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
