"""Fast-lane protection: PositionCache (WS-fed mirror) + RiskEngine (tick-driven).

The RiskEngine replaces the 15-min cycle enforcement: on every mark-price tick
for a held symbol it checks, in priority order,
  1. liquidation guard  — force-close before the exchange liquidates
  2. hard stop-loss     — ROE ≤ per-trade sl_pct
  3. take-profit        — ROE ≥ per-trade tp_pct
  4. martingale add     — only ≤ MARTINGALE_MAX_LEVERAGE, with level/interval gates

Closes go through execution.close_position() on the engine's OWN Binance client
(requests.Session is not thread-safe — one Client per thread).
"""
from __future__ import annotations
import logging
import queue
import threading
import time
from dataclasses import dataclass, replace

from config import CFG
from events import Trigger, TriggerBus
import execution
import journal
from stream import MarketState


log = logging.getLogger("bot.risk")


def liq_guard_price(entry: float, liquidation: float, fraction: float) -> float:
    """Price at which the guard force-closes: `fraction` of the way from entry
    to the liquidation price. Works for both sides (liq < entry for LONG,
    liq > entry for SHORT)."""
    return entry + fraction * (liquidation - entry)


def crossed_guard(side: str, mark: float, guard: float) -> bool:
    return mark <= guard if side == "LONG" else mark >= guard


@dataclass
class CachedPosition:
    symbol: str
    side: str                 # "LONG" | "SHORT"
    qty: float
    entry_price: float
    isolated_margin: float
    leverage: int
    sl_pct: float             # ROE-based, negative
    tp_pct: float             # ROE-based, positive
    liquidation_price: float  # exchange-computed; 0 = unknown → estimate
    martingale_levels: int = 0
    last_add_monotonic: float = 0.0


class PositionCache:
    """Thread-safe mirror of open positions.

    Sources of truth, in order of authority:
    - REST reconcile (seed, periodic watchdog pass, after every execution)
    - ACCOUNT_UPDATE user-stream events (real-time qty/entry/margin deltas)
    Per-trade targets (sl/tp) come from the journal at reconcile time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._positions: dict[str, CachedPosition] = {}
        self.needs_reconcile = threading.Event()

    # ---- reads ----
    def get(self, symbol: str) -> CachedPosition | None:
        with self._lock:
            return self._positions.get(symbol)

    def held_symbols(self) -> set[str]:
        with self._lock:
            return set(self._positions)

    def snapshot(self) -> list[CachedPosition]:
        with self._lock:
            return [replace(p) for p in self._positions.values()]

    # ---- mutations ----
    def remove(self, symbol: str) -> None:
        with self._lock:
            self._positions.pop(symbol, None)

    def mark_martingale_add(self, symbol: str) -> None:
        with self._lock:
            p = self._positions.get(symbol)
            if p:
                p.martingale_levels += 1
                p.last_add_monotonic = time.monotonic()

    def reconcile(self, client) -> None:
        """Re-pull REST truth. Called from main thread (post-execution / cycle)
        and from the watchdog thread (its own client) — never from WS callbacks."""
        fresh: dict[str, CachedPosition] = {}
        for p in execution.get_open_positions(client):
            sl_pct, tp_pct = journal.get_position_targets(p.symbol)
            prev = self.get(p.symbol)
            fresh[p.symbol] = CachedPosition(
                symbol=p.symbol, side=p.side, qty=p.qty,
                entry_price=p.entry_price, isolated_margin=p.isolated_margin,
                leverage=p.leverage, sl_pct=sl_pct, tp_pct=tp_pct,
                liquidation_price=p.liquidation_price,
                martingale_levels=execution.count_martingale_levels(p.symbol),
                last_add_monotonic=prev.last_add_monotonic if prev else 0.0,
            )
        with self._lock:
            gone = set(self._positions) - set(fresh)
            new = set(fresh) - set(self._positions)
            self._positions = fresh
        self.needs_reconcile.clear()
        if gone or new:
            log.info(f"position cache reconciled: +{sorted(new)} -{sorted(gone)}")

    # ---- WS-fed updates (called from the user-stream callback: must not block) ----
    def apply_account_update(self, account_payload: dict) -> None:
        for p in account_payload.get("P", []):
            try:
                symbol = p["s"]
                amt = float(p["pa"])
            except (KeyError, TypeError, ValueError):
                continue
            with self._lock:
                cached = self._positions.get(symbol)
                if amt == 0:
                    self._positions.pop(symbol, None)
                    continue
                if cached is None:
                    # Position we don't know yet (opened by the main thread an
                    # instant ago, or out-of-band): needs a REST pass for
                    # leverage/targets. Flag it — never call REST from here.
                    self.needs_reconcile.set()
                    continue
                cached.side = "LONG" if amt > 0 else "SHORT"
                cached.qty = abs(amt)
                try:
                    entry = float(p.get("ep") or 0)
                    if entry > 0:
                        cached.entry_price = entry
                    iw = float(p.get("iw") or 0)
                    if iw > 0:
                        cached.isolated_margin = iw
                except (TypeError, ValueError):
                    pass


class RiskEngine(threading.Thread):
    """Consumes the tick queue and enforces protection in <1s from the tick."""

    def __init__(self, cache: PositionCache, state: MarketState,
                 tick_queue: "queue.Queue[str]", bus: TriggerBus,
                 initial_wallet: float):
        super().__init__(name="risk-engine", daemon=True)
        self.cache = cache
        self.state = state
        self.tick_queue = tick_queue
        self.bus = bus
        self.stop_event = threading.Event()
        self._client = None
        # per-symbol close latch: monotonic of last close attempt
        self._last_close: dict[str, float] = {}
        self._in_flight: set[str] = set()
        # coarse equity floor tracking
        self.wallet_balance = initial_wallet
        self._last_floor_check = 0.0
        self._kill_cache: tuple[float, bool] = (0.0, False)

    # ---- helpers ----
    def _kill_switch_active(self) -> bool:
        now = time.monotonic()
        ts, val = self._kill_cache
        if now - ts > 5.0:
            self._kill_cache = (now, CFG.KILL_SWITCH.exists())
        return self._kill_cache[1]

    def apply_wallet_update(self, account_payload: dict) -> None:
        """Fed by the user stream (via UserStream.on_account_update wrapper)."""
        for b in account_payload.get("B", []):
            if b.get("a") == "USDT":
                try:
                    self.wallet_balance = float(b["wb"])
                except (KeyError, TypeError, ValueError):
                    pass

    def _to_position(self, c: CachedPosition, mark: float) -> execution.Position:
        direction = 1.0 if c.side == "LONG" else -1.0
        pnl = (mark - c.entry_price) * c.qty * direction
        roe = pnl / c.isolated_margin if c.isolated_margin else 0.0
        return execution.Position(
            symbol=c.symbol, side=c.side, qty=c.qty,
            entry_price=c.entry_price, mark_price=mark,
            unrealized_pnl=pnl, unrealized_pnl_pct=roe,
            leverage=c.leverage, isolated_margin=c.isolated_margin,
            liquidation_price=c.liquidation_price,
            martingale_levels=c.martingale_levels,
        )

    def _latched(self, symbol: str) -> bool:
        now = time.monotonic()
        if symbol in self._in_flight:
            return True
        return now - self._last_close.get(symbol, -1e9) < CFG.RISK_MIN_CLOSE_INTERVAL_SECONDS

    def _close(self, cached: CachedPosition, mark: float, reason: str, detail: str) -> None:
        symbol = cached.symbol
        self._in_flight.add(symbol)
        self._last_close[symbol] = time.monotonic()
        try:
            pos = self._to_position(cached, mark)
            log.warning(f"RISK {reason.upper()} {symbol} roe={pos.unrealized_pnl_pct:+.2%} "
                        f"mark={mark} {detail}")
            execution.close_position(self._client, pos, reason=reason,
                                     trigger=f"risk:{reason}")
            self.cache.remove(symbol)
            self.bus.emit(Trigger(kind="risk_exit", symbol=symbol,
                                  detail=f"{reason} @ roe {pos.unrealized_pnl_pct:+.1%}"))
        except Exception as e:
            # -2022 ReduceOnly rejected = position already gone (race) — benign
            if "-2022" in str(e):
                log.info(f"{symbol}: close race (-2022), position already flat")
                self.cache.remove(symbol)
            else:
                log.error(f"risk close {symbol} failed: {e}")
                journal.log_event("ERROR", f"risk close {symbol}: {e}")
        finally:
            self._in_flight.discard(symbol)

    # ---- per-tick rule evaluation ----
    def _check(self, symbol: str) -> None:
        cached = self.cache.get(symbol)
        if cached is None or self._latched(symbol):
            return
        mark = self.state.price(symbol)
        if mark is None or mark <= 0 or cached.isolated_margin <= 0:
            return

        direction = 1.0 if cached.side == "LONG" else -1.0
        pnl = (mark - cached.entry_price) * cached.qty * direction
        roe = pnl / cached.isolated_margin

        # (1) liquidation guard — prefer the exchange's own liquidation price
        if cached.liquidation_price > 0 and cached.entry_price > 0:
            guard = liq_guard_price(cached.entry_price, cached.liquidation_price,
                                    CFG.RISK_LIQ_GUARD_FRACTION)
            if crossed_guard(cached.side, mark, guard):
                self._close(cached, mark, "liq_guard",
                            f"guard={guard:.6g} liq={cached.liquidation_price:.6g}")
                return
        else:  # fallback: estimated distance
            adverse = (cached.entry_price - mark) / cached.entry_price * direction
            if adverse >= CFG.RISK_LIQ_GUARD_FRACTION * execution.estimated_liq_distance(cached.leverage):
                self._close(cached, mark, "liq_guard", "estimated liq distance")
                return

        # (2) hard stop-loss
        if roe <= cached.sl_pct:
            self._close(cached, mark, "sl", f"target {cached.sl_pct:+.0%}")
            return

        # (3) take-profit
        if roe >= cached.tp_pct:
            self._close(cached, mark, "tp", f"target {cached.tp_pct:+.0%}")
            return

        # (4) martingale — deterministic averaging, moderate leverage only,
        #     and never while the kill switch asks the bot to stand down
        if (cached.leverage <= CFG.MARTINGALE_MAX_LEVERAGE
                and roe <= CFG.MARTINGALE_TRIGGER_ROE
                and cached.martingale_levels < CFG.MARTINGALE_MAX_LEVELS
                and time.monotonic() - cached.last_add_monotonic >= CFG.MARTINGALE_MIN_INTERVAL_SECONDS
                and not self._kill_switch_active()):
            self._in_flight.add(symbol)
            try:
                log.info(f"RISK MARTINGALE {symbol} level={cached.martingale_levels + 1} roe={roe:+.2%}")
                pos = self._to_position(cached, mark)
                if execution.add_martingale(self._client, pos) is not None:
                    self.cache.mark_martingale_add(symbol)
                    self.cache.needs_reconcile.set()  # entry/margin changed
            except Exception as e:
                log.error(f"martingale {symbol} failed: {e}")
            finally:
                self._in_flight.discard(symbol)

    def _check_equity_floor(self) -> None:
        now = time.monotonic()
        if now - self._last_floor_check < 10.0:
            return
        self._last_floor_check = now
        unrealized = 0.0
        for c in self.cache.snapshot():
            mark = self.state.price(c.symbol)
            if mark:
                direction = 1.0 if c.side == "LONG" else -1.0
                unrealized += (mark - c.entry_price) * c.qty * direction
        equity_est = self.wallet_balance + unrealized
        floor = CFG.INITIAL_CAPITAL_USDT * CFG.EQUITY_FLOOR_PCT
        if equity_est < floor and not CFG.KILL_SWITCH.exists():
            log.critical(f"EQUITY FLOOR (real-time) — est. equity {equity_est:.2f} < {floor:.2f}")
            journal.log_event("HALT", f"equity floor breached (risk engine, est={equity_est:.2f})")
            CFG.KILL_SWITCH.write_text("auto-halt: equity floor breached\n", encoding="utf-8")

    # ---- thread body ----
    def run(self) -> None:
        while self._client is None and not self.stop_event.is_set():
            try:
                self._client = execution.make_client()
            except Exception as e:
                log.error(f"risk engine client init failed, retrying in 5s: {e}")
                self.stop_event.wait(5.0)
        log.info("risk engine online")
        while not self.stop_event.is_set():
            try:
                symbol = self.tick_queue.get(timeout=1.0)
            except queue.Empty:
                self._check_equity_floor()
                continue
            try:
                self._check(symbol)
            except Exception as e:
                log.error(f"risk check {symbol} failed: {e}")
            self._check_equity_floor()
