"""SQLite journal: every decision, trade, equity snapshot, and event lands here."""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
    note TEXT
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(CFG.JOURNAL_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    CFG.JOURNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)


def log_decision(market_view: str, decisions: list[dict], raw: str | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO decisions (ts, market_view, decisions_json, raw_response) VALUES (?, ?, ?, ?)",
            (_now(), market_view, json.dumps(decisions), raw),
        )


def log_trade(symbol: str, side: str, qty: float, price: float, notional: float,
              leverage: int, kind: str, note: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, notional_usdt, leverage, kind, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, side, qty, price, notional, leverage, kind, note),
        )


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
