"""Shadow strategies: paper-track 3 multi-crypto strategies on real prices.

Each strategy holds a 5-crypto blue-chip portfolio (BLUE_CHIP_PORTFOLIO):
- shadow_hodl     : open 5 longs at first cycle (allocation/5 each), hold forever.
- shadow_dca      : weekly DCA into all 5 (allocation/5/8 per crypto per week, max 8 weeks).
- shadow_lowlev   : open 5 longs at 2x leverage at first cycle, hold forever.

State is reconstructed from JSON-encoded events on each call (idempotent).
The aggregate equity for each strategy is logged to the equity table.
A per-symbol breakdown is exposed via get_breakdown() for the dashboard.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

import data
from config import CFG, STRATEGY_ALLOCATIONS, BLUE_CHIP_PORTFOLIO
import journal


HODL_USDT = STRATEGY_ALLOCATIONS.get("hodl", 0.0)
DCA_USDT = STRATEGY_ALLOCATIONS.get("dca", 0.0)
CONS_USDT = STRATEGY_ALLOCATIONS.get("conservative_2x", 0.0)
N = len(BLUE_CHIP_PORTFOLIO)
_ANY_ENABLED = (HODL_USDT + DCA_USDT + CONS_USDT) > 0


def _events_with_level(level: str) -> list[sqlite3.Row]:
    with sqlite3.connect(CFG.JOURNAL_DB) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT id, ts, msg FROM events WHERE level=? ORDER BY id ASC",
            (level,),
        ).fetchall()


def _all_prices() -> dict[str, float]:
    return {sym: data.get_price(sym) for sym in BLUE_CHIP_PORTFOLIO}


# ---------------------------------------------------------------------------
# HODL: open 5 longs at allocation/5 each on first cycle, hold forever.
# ---------------------------------------------------------------------------
def _hodl_state(prices: dict[str, float]) -> dict[str, dict[str, float]]:
    rows = _events_with_level("SHADOW_HODL_OPEN")
    if rows:
        return json.loads(rows[0]["msg"])
    per_sym = HODL_USDT / N
    state = {sym: {"qty": per_sym / prices[sym], "entry": prices[sym]}
             for sym in BLUE_CHIP_PORTFOLIO}
    journal.log_event("SHADOW_HODL_OPEN", json.dumps(state))
    return state


def hodl_breakdown(prices: dict[str, float]) -> tuple[float, list[dict[str, Any]]]:
    state = _hodl_state(prices)
    rows = []
    total = 0.0
    for sym, pos in state.items():
        cur = prices[sym]
        value = pos["qty"] * cur
        pnl = value - pos["qty"] * pos["entry"]
        pnl_pct = (cur - pos["entry"]) / pos["entry"] if pos["entry"] else 0.0
        rows.append({
            "symbol": sym, "qty": pos["qty"], "entry": pos["entry"],
            "price": cur, "allocation": HODL_USDT / N,
            "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
        })
        total += value
    return total, rows


# ---------------------------------------------------------------------------
# DCA: weekly buy 1/8 of allocation into each crypto. State = list of buys.
# ---------------------------------------------------------------------------
def _dca_buys() -> list[dict[str, Any]]:
    rows = _events_with_level("SHADOW_DCA_WEEK")
    return [json.loads(r["msg"]) for r in rows]


def _dca_buy_if_due(prices: dict[str, float]) -> list[dict[str, Any]]:
    buys = _dca_buys()
    if len(buys) >= 8:
        return buys
    now = datetime.now(timezone.utc)
    if buys:
        last_ts = datetime.fromisoformat(buys[-1]["ts"])
        if now - last_ts < timedelta(days=7):
            return buys

    chunk_per_sym = DCA_USDT / N / 8
    fills = {sym: {"qty": chunk_per_sym / prices[sym], "entry": prices[sym]}
             for sym in BLUE_CHIP_PORTFOLIO}
    week = {"ts": now.isoformat(), "chunk_per_sym": chunk_per_sym, "fills": fills}
    journal.log_event("SHADOW_DCA_WEEK", json.dumps(week))
    return _dca_buys()


def dca_breakdown(prices: dict[str, float]) -> tuple[float, list[dict[str, Any]]]:
    buys = _dca_buy_if_due(prices)
    accum_qty: dict[str, float] = {sym: 0.0 for sym in BLUE_CHIP_PORTFOLIO}
    cost: dict[str, float] = {sym: 0.0 for sym in BLUE_CHIP_PORTFOLIO}
    for w in buys:
        for sym, fill in w["fills"].items():
            accum_qty[sym] += fill["qty"]
            cost[sym] += fill["qty"] * fill["entry"]
    cash_per_sym_left = (DCA_USDT / N) - sum(w["chunk_per_sym"] for w in buys)
    cash_per_sym_left = max(cash_per_sym_left, 0.0)

    rows = []
    total = 0.0
    for sym in BLUE_CHIP_PORTFOLIO:
        cur = prices[sym]
        crypto_value = accum_qty[sym] * cur
        avg_entry = (cost[sym] / accum_qty[sym]) if accum_qty[sym] > 0 else 0.0
        sym_total = crypto_value + cash_per_sym_left
        pnl = sym_total - (DCA_USDT / N)
        pnl_pct = pnl / (DCA_USDT / N) if DCA_USDT else 0.0
        rows.append({
            "symbol": sym, "qty": accum_qty[sym], "entry": avg_entry,
            "price": cur, "allocation": DCA_USDT / N,
            "value": sym_total, "pnl": pnl, "pnl_pct": pnl_pct,
            "weeks_filled": len(buys),
        })
        total += sym_total
    return total, rows


# ---------------------------------------------------------------------------
# Conservative 2x: 5 longs at 2x leverage opened on first cycle, hold.
# ---------------------------------------------------------------------------
def _cons_state(prices: dict[str, float]) -> dict[str, dict[str, float]]:
    rows = _events_with_level("SHADOW_CONS_OPEN")
    if rows:
        return json.loads(rows[0]["msg"])
    margin_per_sym = CONS_USDT / N
    leverage = 2
    state = {}
    for sym in BLUE_CHIP_PORTFOLIO:
        notional = margin_per_sym * leverage
        state[sym] = {
            "qty": notional / prices[sym],
            "entry": prices[sym],
            "margin": margin_per_sym,
            "leverage": leverage,
        }
    journal.log_event("SHADOW_CONS_OPEN", json.dumps(state))
    return state


def cons_breakdown(prices: dict[str, float]) -> tuple[float, list[dict[str, Any]]]:
    state = _cons_state(prices)
    rows = []
    total = 0.0
    for sym, pos in state.items():
        cur = prices[sym]
        # P&L on collateral: notional move scaled by leverage
        pnl = (cur - pos["entry"]) * pos["qty"]
        sym_total = pos["margin"] + pnl
        pnl_pct = pnl / pos["margin"] if pos["margin"] else 0.0
        rows.append({
            "symbol": sym, "qty": pos["qty"], "entry": pos["entry"],
            "price": cur, "allocation": pos["margin"],
            "value": sym_total, "pnl": pnl, "pnl_pct": pnl_pct,
            "leverage": pos["leverage"],
        })
        total += sym_total
    return total, rows


# ---------------------------------------------------------------------------
# Top-level update: log aggregate equity for each shadow.
# ---------------------------------------------------------------------------
def update_shadows() -> None:
    if not _ANY_ENABLED:
        return
    prices = _all_prices()

    hodl_total, _ = hodl_breakdown(prices)
    journal.log_equity(wallet=hodl_total, unrealized=0.0, equity=hodl_total,
                       open_positions=N, source="shadow_hodl")

    dca_total, _ = dca_breakdown(prices)
    journal.log_equity(wallet=dca_total, unrealized=0.0, equity=dca_total,
                       open_positions=N, source="shadow_dca")

    cons_total, _ = cons_breakdown(prices)
    journal.log_equity(wallet=cons_total, unrealized=0.0, equity=cons_total,
                       open_positions=N, source="shadow_lowlev")


# ---------------------------------------------------------------------------
# For dashboard: get the granular breakdown without forcing new buys/inits.
# ---------------------------------------------------------------------------
def get_all_breakdowns() -> dict[str, list[dict[str, Any]]]:
    """Return per-strategy per-symbol details. Calls update_shadows-equivalent
    state functions which are idempotent (init-on-first-call only)."""
    if not _ANY_ENABLED:
        return {"hodl": [], "dca": [], "conservative_2x": []}
    prices = _all_prices()
    _, hodl_rows = hodl_breakdown(prices)
    _, dca_rows = dca_breakdown(prices)
    _, cons_rows = cons_breakdown(prices)
    return {"hodl": hodl_rows, "dca": dca_rows, "conservative_2x": cons_rows}
