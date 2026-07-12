"""WebSocket layer: mainnet market data, testnet user stream, shared state, watchdog.

Threading contract (load-bearing):
- ThreadedWebsocketManager callbacks run on the TWM's own asyncio loop thread.
  They must NEVER block — no REST calls, no SQLite. They only update MarketState,
  push symbols onto the tick queue, and emit Triggers.
- Two separate TWM instances: mainnet (public market data, no keys) and
  testnet (authenticated user stream). Never mix them.
- A TWM that exhausted its 5 reconnect attempts is dead for good and its thread
  can't be restarted — the Watchdog builds a FRESH instance instead.
"""
from __future__ import annotations
import asyncio
import logging
import os
import queue
import threading
import time
from collections import deque

import requests
from binance import ThreadedWebsocketManager

from config import CFG
from events import Trigger, TriggerBus
import journal


log = logging.getLogger("bot.stream")

_PUBLIC_BASE = "https://fapi.binance.com"  # market data always from mainnet (same as data.py)


# ============================================================================
# MarketState — thread-safe latest-tick store + short price history
# ============================================================================
class MarketState:
    """Latest mark price / funding per symbol + bounded price history for the
    watch set (held ∪ watchlist). History is what PRICE_MOVE detection reads."""

    def __init__(self):
        self._lock = threading.Lock()
        # symbol -> (mark_price, funding_rate, event_ms, recv_monotonic)
        self._last: dict[str, tuple[float, float, int, float]] = {}
        # symbol -> deque[(recv_monotonic, price)] — only for watched symbols
        self._history: dict[str, deque[tuple[float, float]]] = {}
        self._watch: set[str] = set()
        self._held: set[str] = set()
        # ring sized for ~20 min at 1 tick/s
        self._history_len = max(int(CFG.EVENT_PRICE_MOVE_WINDOW_SECONDS * 1.4), 600)

    # ---- watch/held management (called from main / risk engine) ----
    def set_watch(self, symbols: set[str]) -> None:
        with self._lock:
            self._watch = set(symbols) | self._held
            for sym in list(self._history):
                if sym not in self._watch:
                    del self._history[sym]

    def set_held(self, symbols: set[str]) -> None:
        with self._lock:
            self._held = set(symbols)
            self._watch |= self._held

    def watch(self) -> set[str]:
        with self._lock:
            return set(self._watch)

    def is_held(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._held

    # ---- updates (called from WS callback / REST fallback) ----
    def update(self, symbol: str, price: float, funding: float | None, event_ms: int) -> None:
        now = time.monotonic()
        with self._lock:
            prev = self._last.get(symbol)
            self._last[symbol] = (price, funding if funding is not None
                                  else (prev[1] if prev else 0.0), event_ms, now)
            if symbol in self._watch:
                hist = self._history.get(symbol)
                if hist is None:
                    hist = self._history[symbol] = deque(maxlen=self._history_len)
                hist.append((now, price))

    # ---- reads ----
    def get(self, symbol: str) -> tuple[float, float, int, float] | None:
        with self._lock:
            return self._last.get(symbol)

    def price(self, symbol: str) -> float | None:
        entry = self.get(symbol)
        return entry[0] if entry else None

    def funding(self, symbol: str) -> float | None:
        entry = self.get(symbol)
        return entry[1] if entry else None

    def age_seconds(self, symbol: str | None = None) -> float:
        """Age of the freshest tick (symbol=None → freshest across the watch set).
        inf when no tick was ever received."""
        now = time.monotonic()
        with self._lock:
            if symbol is not None:
                entry = self._last.get(symbol)
                return now - entry[3] if entry else float("inf")
            recents = [self._last[s][3] for s in self._watch if s in self._last]
            return now - max(recents) if recents else float("inf")

    def pct_move(self, symbol: str, window_seconds: float) -> float | None:
        """Signed fractional move over the window, None if history too thin."""
        now = time.monotonic()
        with self._lock:
            hist = self._history.get(symbol)
            if not hist or len(hist) < 2:
                return None
            latest_ts, latest_price = hist[-1]
            base_price = None
            for ts, price in hist:
                if now - ts <= window_seconds:
                    base_price = price
                    break
            if base_price is None or base_price <= 0:
                return None
            return (latest_price - base_price) / base_price


# ============================================================================
# MarketStream — mainnet !markPrice@arr@1s
# ============================================================================
class MarketStream:
    """Owns the mainnet TWM. One static all-market subscription: no resubscribe
    churn when the universe rotates or positions open/close."""

    def __init__(self, state: MarketState, bus: TriggerBus, tick_queue: queue.Queue):
        self.state = state
        self.bus = bus
        self.tick_queue = tick_queue
        self.dead = threading.Event()
        self._twm: ThreadedWebsocketManager | None = None
        # (symbol, kind) -> monotonic of last emitted trigger (anti-spam)
        self._last_trigger: dict[tuple[str, str], float] = {}
        self._funding_sign: dict[str, int] = {}

    def start(self) -> None:
        self.dead.clear()
        # Dedicated event loop per TWM: two instances in one process would
        # otherwise share the caller's default loop and the second dies with
        # "This event loop is already running". set_event_loop is ALSO required:
        # ReconnectingWebsocket.__init__ (created below, in THIS thread) grabs
        # the calling thread's default loop and schedules its read-loop there —
        # if that isn't the TWM's own loop, the socket connects but stays mute.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._twm = ThreadedWebsocketManager(testnet=False, loop=loop)
        self._twm.start()
        self._twm.start_all_mark_price_socket(callback=self._on_message, fast=True)
        log.info("market stream started (mainnet !markPrice@arr@1s)")

    def stop(self) -> None:
        if self._twm is not None:
            try:
                self._twm.stop()
            except Exception as e:
                log.warning(f"market stream stop: {e}")
            self._twm = None

    def restart(self) -> None:
        self.stop()
        self.start()

    # ---- callback (TWM loop thread — never block) ----
    def _on_message(self, msg) -> None:
        if isinstance(msg, dict):
            if msg.get("e") == "error":
                self.dead.set()
                return
            msg = msg.get("data", msg)
        if not isinstance(msg, list):
            return
        watch = self.state.watch()
        for entry in msg:
            try:
                sym = entry["s"]
                price = float(entry["p"])
                funding = float(entry.get("r") or 0.0)
                event_ms = int(entry.get("E") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            self.state.update(sym, price, funding, event_ms)
            if sym not in watch:
                continue
            try:
                self.tick_queue.put_nowait(sym)
            except queue.Full:
                pass  # risk engine backlogged; ticks are ephemeral, drop
            self._detect_price_move(sym)
            self._detect_funding(sym, funding)

    def _cooldown_ok(self, sym: str, kind: str) -> bool:
        now = time.monotonic()
        last = self._last_trigger.get((sym, kind), -1e9)
        if now - last < CFG.EVENT_MIN_CALL_INTERVAL_SECONDS:
            return False
        self._last_trigger[(sym, kind)] = now
        return True

    def _detect_price_move(self, sym: str) -> None:
        move = self.state.pct_move(sym, CFG.EVENT_PRICE_MOVE_WINDOW_SECONDS)
        if move is None:
            return
        threshold = (CFG.EVENT_PRICE_MOVE_PCT_HELD if self.state.is_held(sym)
                     else CFG.EVENT_PRICE_MOVE_PCT_WATCHLIST)
        if abs(move) >= threshold and self._cooldown_ok(sym, "price_move"):
            self.bus.emit(Trigger(kind="price_move", symbol=sym,
                                  detail=f"{move:+.2%} in {CFG.EVENT_PRICE_MOVE_WINDOW_SECONDS//60}min"))

    def _detect_funding(self, sym: str, funding: float) -> None:
        if not self.state.is_held(sym):
            return
        prev_sign = self._funding_sign.get(sym)
        sign = 1 if funding > 0 else (-1 if funding < 0 else 0)
        self._funding_sign[sym] = sign
        flipped = prev_sign is not None and sign != 0 and prev_sign != 0 and sign != prev_sign
        extreme = abs(funding) > CFG.EVENT_FUNDING_ABS_THRESHOLD
        if (flipped or extreme) and self._cooldown_ok(sym, "funding_flip"):
            self.bus.emit(Trigger(kind="funding_flip", symbol=sym,
                                  detail=f"funding={funding:+.4%}" + (" (sign flip)" if flipped else "")))


# ============================================================================
# UserStream — testnet account/order events
# ============================================================================
class UserStream:
    """Owns the testnet TWM (authenticated). listenKey keepalive is handled by
    python-binance automatically (every 5 min). Phase 2: log-only; the position
    cache consumer is attached in Phase 3 via `on_order_fill` / `on_account_update`."""

    def __init__(self):
        self.dead = threading.Event()
        self._twm: ThreadedWebsocketManager | None = None
        self.last_event_monotonic: float = time.monotonic()
        # Phase-3 hooks (set by risk engine); must be non-blocking.
        self.on_order_fill = None       # callable(order_dict) | None
        self.on_account_update = None   # callable(account_dict) | None

    def start(self) -> None:
        self.dead.clear()
        # Own loop + set_event_loop in the calling thread — see MarketStream.start
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._twm = ThreadedWebsocketManager(
            api_key=CFG.BINANCE_API_KEY, api_secret=CFG.BINANCE_API_SECRET,
            testnet=CFG.USE_TESTNET,
            loop=loop,
        )
        self._twm.start()
        self._twm.start_futures_user_socket(callback=self._on_message)
        log.info(f"user stream started (testnet={CFG.USE_TESTNET})")

    def stop(self) -> None:
        if self._twm is not None:
            try:
                self._twm.stop()
            except Exception as e:
                log.warning(f"user stream stop: {e}")
            self._twm = None

    def restart(self) -> None:
        self.stop()
        self.start()

    # ---- callback (TWM loop thread — never block) ----
    def _on_message(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        if msg.get("e") == "error":
            self.dead.set()
            return
        self.last_event_monotonic = time.monotonic()
        event = msg.get("e")
        if event == "ORDER_TRADE_UPDATE":
            o = msg.get("o", {})
            if o.get("X") in ("FILLED", "PARTIALLY_FILLED"):
                log.info(
                    f"WS fill: {o.get('s')} {o.get('S')} {o.get('o')} qty={o.get('l')} "
                    f"avg={o.get('ap')} reduceOnly={o.get('R')} status={o.get('X')} rp={o.get('rp')}"
                )
                if self.on_order_fill is not None:
                    try:
                        self.on_order_fill(o)
                    except Exception as e:
                        log.warning(f"on_order_fill: {e}")
        elif event == "ACCOUNT_UPDATE":
            a = msg.get("a", {})
            n_pos = len(a.get("P", []))
            log.info(f"WS account update: reason={a.get('m')} positions_touched={n_pos}")
            if self.on_account_update is not None:
                try:
                    self.on_account_update(a)
                except Exception as e:
                    log.warning(f"on_account_update: {e}")
        elif event == "listenKeyExpired":
            # library normally refreshes in time; treat as dead so watchdog rebuilds
            log.warning("listenKey expired — flagging user stream for restart")
            self.dead.set()


# ============================================================================
# Watchdog — stale detection, REST fallback, process recycle
# ============================================================================
class Watchdog(threading.Thread):
    """Daemon: keeps the streams honest.

    - market ticks stale > WS_STALE_SECONDS → restart MarketStream (fresh TWM)
      and feed MarketState via REST /premiumIndex until ticks resume
    - user stream flagged dead → restart it
    - a stream that fails to revive twice in a row → FATAL journal event +
      os._exit(1) so runner.py + Railway restart the whole process
    - optional `reconcile` callback (Phase 3: PositionCache vs REST truth)
    """

    def __init__(self, market_stream: MarketStream, user_stream: UserStream,
                 state: MarketState, reconcile=None,
                 reconcile_asap: threading.Event | None = None):
        super().__init__(name="watchdog", daemon=True)
        self.market_stream = market_stream
        self.user_stream = user_stream
        self.state = state
        self.reconcile = reconcile
        self.reconcile_asap = reconcile_asap  # set by PositionCache on unknown WS positions
        self.stop_event = threading.Event()
        self._market_revive_failures = 0
        self._user_revive_failures = 0
        self._last_reconcile = time.monotonic()
        self._rest_fallback_active = False

    def run(self) -> None:
        while not self.stop_event.wait(5.0):
            try:
                self._check_market()
                self._check_user()
                self._maybe_reconcile()
            except Exception as e:
                log.error(f"watchdog iteration failed: {e}")

    def _check_market(self) -> None:
        age = self.state.age_seconds()
        stale = age > CFG.WS_STALE_SECONDS or self.market_stream.dead.is_set()
        if not stale:
            self._market_revive_failures = 0
            if self._rest_fallback_active:
                log.info("market stream recovered — REST fallback off")
                self._rest_fallback_active = False
            return

        if not self._rest_fallback_active:
            log.warning(f"WS_STALE: no market tick for {age:.0f}s — restarting stream, REST fallback on")
            journal.log_event("WS_STALE", f"market ticks stale {age:.0f}s")
            self._rest_fallback_active = True

        self._rest_poll_prices()

        try:
            self.market_stream.restart()
            time.sleep(3)  # give the fresh socket a moment before re-judging
            if self.state.age_seconds() <= CFG.WS_STALE_SECONDS:
                self._market_revive_failures = 0
            else:
                self._market_revive_failures += 1
        except Exception as e:
            log.error(f"market stream restart failed: {e}")
            self._market_revive_failures += 1

        if self._market_revive_failures >= 2:
            journal.log_event("FATAL", "market stream unrecoverable — recycling process")
            log.critical("market stream unrecoverable — exiting for supervisor restart")
            os._exit(1)

    def _check_user(self) -> None:
        if not self.user_stream.dead.is_set():
            self._user_revive_failures = 0
            return
        log.warning("user stream flagged dead — restarting")
        journal.log_event("WS_STALE", "user stream dead — restart")
        try:
            self.user_stream.restart()
            self._user_revive_failures = 0
        except Exception as e:
            log.error(f"user stream restart failed: {e}")
            self._user_revive_failures += 1
        if self._user_revive_failures >= 2:
            journal.log_event("FATAL", "user stream unrecoverable — recycling process")
            os._exit(1)

    def _rest_poll_prices(self) -> None:
        """Feed MarketState from mainnet REST while the WS is down, so the risk
        engine keeps protecting positions (at fallback-poll resolution)."""
        try:
            r = requests.get(f"{_PUBLIC_BASE}/fapi/v1/premiumIndex", timeout=10)
            r.raise_for_status()
            watch = self.state.watch()
            for entry in r.json():
                sym = entry.get("symbol")
                if sym in watch:
                    self.state.update(sym, float(entry["markPrice"]),
                                      float(entry.get("lastFundingRate") or 0.0),
                                      int(entry.get("time") or 0))
        except Exception as e:
            log.warning(f"REST price fallback failed: {e}")

    def _maybe_reconcile(self) -> None:
        if self.reconcile is None:
            return
        now = time.monotonic()
        asap = self.reconcile_asap is not None and self.reconcile_asap.is_set()
        if asap or now - self._last_reconcile >= CFG.RECONCILE_INTERVAL_SECONDS:
            self._last_reconcile = now
            try:
                self.reconcile()
            except Exception as e:
                log.warning(f"reconcile failed: {e}")
