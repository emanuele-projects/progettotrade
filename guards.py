"""Deterministic entry guards + drawdown brake (2026-07-19 post-mortem).

Week 1 taught two lessons the prompt alone could not enforce:

  1. CHURN KILLS. On chop days the loop stop-fires → instant refill → the same
     mover re-entered (up to 17x/day) → stop again. ~80 opens/day and ~$150/day
     of commissions ate 100% of the gross trade P&L. The always-invested mandate
     needs a brake pedal, in CODE.
  2. PEAKS MUST BE DEFENDED. Equity ran +33% to $5,210 then round-tripped to
     flat in 36h with nothing slowing the give-back. A high-water-mark brake
     that de-risks the book on an -8% drawdown keeps a winning week won.

Everything here is enforced in execute_decisions — Claude is INFORMED via the
portfolio status (so its reasoning stays coherent) but cannot override it.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from config import CFG
import journal


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class EntryGuard:
    """Per-cycle gate for new entries: cooldown after a losing stop, per-symbol
    and global daily open budgets, failure blacklist, defensive de-risking.

    One instance per bot process; `defensive` is refreshed each cycle."""

    def __init__(self) -> None:
        self.defensive = False
        self._failures: dict[str, int] = {}
        self._blacklist_until: dict[str, float] = {}  # monotonic deadline

    # ---- deny / allow -----------------------------------------------------
    def check(self, symbol: str, n_open: int) -> str | None:
        """Return a human-readable deny reason, or None if the entry may proceed."""
        until = self._blacklist_until.get(symbol, 0.0)
        if time.monotonic() < until:
            return f"blacklisted after {CFG.ENTRY_FAIL_BLACKLIST_AFTER} failed opens (24h)"

        last_stop = journal.last_losing_exit_ts(symbol)
        if last_stop:
            try:
                age_h = (_now_utc() - datetime.fromisoformat(last_stop)).total_seconds() / 3600
            except ValueError:
                age_h = float("inf")
            if age_h < CFG.REENTRY_COOLDOWN_HOURS_AFTER_STOP:
                return (f"stop-loss exit {age_h:.1f}h ago — re-entry cooldown "
                        f"{CFG.REENTRY_COOLDOWN_HOURS_AFTER_STOP:.0f}h (no churn)")

        midnight = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if journal.count_opens_since(midnight, symbol) >= CFG.MAX_OPENS_PER_SYMBOL_PER_DAY:
            return f"already opened {CFG.MAX_OPENS_PER_SYMBOL_PER_DAY}x today — symbol done for the day"
        if journal.count_opens_since(midnight) >= CFG.MAX_OPENS_PER_DAY:
            return f"daily open budget ({CFG.MAX_OPENS_PER_DAY}) exhausted — churn brake"

        if self.defensive and n_open >= CFG.DEFENSIVE_MIN_POSITIONS:
            return (f"defensive mode: book at {n_open} ≥ reduced minimum "
                    f"{CFG.DEFENSIVE_MIN_POSITIONS} — protecting capital")
        return None

    # ---- sizing adjustments ----------------------------------------------
    def adjust(self, margin_usdt: float, leverage: int) -> tuple[float, int]:
        """Defensive mode: half-size entries, leverage capped hard."""
        if not self.defensive:
            return margin_usdt, leverage
        return (margin_usdt * CFG.DEFENSIVE_MARGIN_FACTOR,
                min(leverage, CFG.DEFENSIVE_MAX_LEVERAGE))

    # ---- failure blacklist ------------------------------------------------
    def record_failure(self, symbol: str) -> None:
        """Called when an open attempt raises (e.g. -4005 maxQty). After N
        consecutive failures the symbol is skipped for 24h instead of being
        retried every cycle (seen live: KAITO rejected 12+ times in a day)."""
        n = self._failures.get(symbol, 0) + 1
        self._failures[symbol] = n
        if n >= CFG.ENTRY_FAIL_BLACKLIST_AFTER:
            self._blacklist_until[symbol] = time.monotonic() + 24 * 3600
            journal.log_event("ENTRY_BLACKLIST", f"{symbol}: {n} failed opens — skipped for 24h")

    def record_success(self, symbol: str) -> None:
        self._failures.pop(symbol, None)


# ---------------------------------------------------------------------------
# Drawdown brake — high-water-mark tracking, persisted across restarts.
# ---------------------------------------------------------------------------
def update_drawdown_state(equity: float, log=None) -> bool:
    """Ratchet the equity peak and flip defensive mode on/off with hysteresis.

    ON  when equity ≤ peak × (1 − DRAWDOWN_BRAKE_PCT)
    OFF when equity ≥ peak × (1 − DRAWDOWN_BRAKE_PCT / 2)
    Peak only ratchets upward (reset it manually via bot_meta on a capital reset).
    Returns the current defensive flag."""
    peak = float(journal.get_meta("equity_peak", "0") or 0)
    if equity > peak:
        peak = equity
        journal.set_meta("equity_peak", f"{equity:.2f}")

    defensive = journal.get_meta("defensive_mode", "0") == "1"
    if peak <= 0:
        return False
    dd = equity / peak - 1

    if not defensive and dd <= -CFG.DRAWDOWN_BRAKE_PCT:
        defensive = True
        journal.set_meta("defensive_mode", "1")
        journal.log_event("DRAWDOWN_BRAKE",
                          f"ON — equity {equity:.2f} is {dd:+.1%} off peak {peak:.2f}: "
                          f"book min→{CFG.DEFENSIVE_MIN_POSITIONS}, lev cap {CFG.DEFENSIVE_MAX_LEVERAGE}x, half-size entries")
        if log is not None:
            log.warning(f"DRAWDOWN BRAKE ON: equity {dd:+.1%} off peak {peak:.2f} — defensive mode")
    elif defensive and dd >= -CFG.DRAWDOWN_BRAKE_PCT / 2:
        defensive = False
        journal.set_meta("defensive_mode", "0")
        journal.log_event("DRAWDOWN_BRAKE", f"OFF — equity recovered to {dd:+.1%} of peak {peak:.2f}")
        if log is not None:
            log.info(f"drawdown brake OFF: equity back to {dd:+.1%} of peak")
    return defensive
