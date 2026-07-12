"""Local signal scanner — the free gatekeeper in front of Claude.

Runs pure Python over data the bot already has (REST features + WS prices),
costs zero API credits, and wakes Claude ONLY when something worth a decision
happens. This replaces the fixed 30-min "re-evaluate everything" clock: at a
flat market the scanner emits nothing and no Claude call is made.

Signals are TRANSITIONS vs the previous scan (an EMA cross, an RSI exit, a
breakout, a position turning against its thesis, a macro-regime shift) — not
static levels — so a persistently-true condition fires once, not every scan.
Each fired signal becomes a Trigger on the shared bus; the main loop batches
those and runs a FOCUSED Claude call on just the involved symbols.
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass

import data
from config import CFG
from events import Trigger, TriggerBus
import journal
from risk_engine import PositionCache
from stream import MarketState


log = logging.getLogger("bot.scanner")


@dataclass
class _Snapshot:
    """The scalar features the scanner compares between scans."""
    above_ema50_4h: bool
    rsi_4h: float
    dist_from_high_30d: float
    dist_from_low_30d: float


def _snapshot(f: data.Features) -> _Snapshot:
    return _Snapshot(
        above_ema50_4h=f.above_ema50_4h,
        rsi_4h=f.rsi_4h,
        dist_from_high_30d=f.dist_from_high_30d,
        dist_from_low_30d=f.dist_from_low_30d,
    )


class SignalScanner(threading.Thread):
    def __init__(self, market_state: MarketState, position_cache: PositionCache,
                 bus: TriggerBus):
        super().__init__(name="signal-scanner", daemon=True)
        self.market_state = market_state
        self.position_cache = position_cache
        self.bus = bus
        self.stop_event = threading.Event()
        self._prev: dict[str, _Snapshot] = {}
        self._last_fired: dict[tuple[str, str], float] = {}  # (symbol, kind) -> monotonic
        # macro state
        self._prev_fg_class: str | None = None
        self._prev_btc_above_ema50_4h: bool | None = None
        # news state
        self._seen_news: set[str] = set()
        self._last_news_poll = 0.0
        self._news_disabled_logged = False

    # ---- debounce ----
    def _fire(self, symbol: str | None, kind: str, detail: str) -> None:
        key = (symbol or "*", kind)
        now = time.monotonic()
        if now - self._last_fired.get(key, -1e9) < CFG.SIGNAL_DEBOUNCE_SECONDS:
            return
        self._last_fired[key] = now
        emitted = self.bus.emit(Trigger(kind="signal" if kind != "news" else "news",
                                        symbol=symbol, detail=f"{kind}: {detail}"))
        if emitted:
            log.info(f"signal {symbol or 'macro'} [{kind}] {detail}")

    # ---- technical scan over watchlist ∪ positions ----
    def _scan_technicals(self) -> None:
        symbols = sorted(self.market_state.watch())
        for sym in symbols:
            if self.stop_event.is_set():
                return
            try:
                f = data.compute_features(sym)
            except Exception as e:
                # non-testnet symbol, transient REST error, etc. — skip, don't spam
                log.debug(f"scanner features {sym}: {e}")
                continue
            prev = self._prev.get(sym)
            self._prev[sym] = _snapshot(f)
            if prev is None:
                continue  # first sighting: establish baseline, emit nothing

            held = self.position_cache.get(sym)

            # (1) EMA50 4h cross — regime change on the swing timeframe
            if f.above_ema50_4h and not prev.above_ema50_4h:
                self._fire(sym, "ema_cross_up", "reclaimed EMA50 on 4h")
            elif not f.above_ema50_4h and prev.above_ema50_4h:
                self._fire(sym, "ema_cross_down", "lost EMA50 on 4h")

            # (2) RSI_4h exiting an extreme (momentum inflection)
            if prev.rsi_4h < CFG.SIGNAL_RSI_OVERSOLD <= f.rsi_4h:
                self._fire(sym, "rsi_exit_oversold", f"RSI_4h {prev.rsi_4h:.0f}→{f.rsi_4h:.0f}")
            elif prev.rsi_4h > CFG.SIGNAL_RSI_OVERBOUGHT >= f.rsi_4h:
                self._fire(sym, "rsi_exit_overbought", f"RSI_4h {prev.rsi_4h:.0f}→{f.rsi_4h:.0f}")

            # (3) 30d-range breakout (crossed into the top/bottom 1% band this scan)
            near = CFG.SIGNAL_BREAKOUT_DIST_30D
            if f.dist_from_high_30d >= -near and prev.dist_from_high_30d < -near:
                self._fire(sym, "breakout_high", f"within {near:.0%} of 30d high")
            elif f.dist_from_low_30d <= near and prev.dist_from_low_30d > near:
                self._fire(sym, "breakdown_low", f"within {near:.0%} of 30d low")

            # (4) open position turning against its thesis (a DECISION signal —
            #     distinct from the risk engine's stop, which is a price event)
            if held is not None:
                if held.side == "LONG" and not f.above_ema50_4h and prev.above_ema50_4h:
                    self._fire(sym, "position_thesis_break", "LONG lost EMA50 4h — review")
                elif held.side == "SHORT" and f.above_ema50_4h and not prev.above_ema50_4h:
                    self._fire(sym, "position_thesis_break", "SHORT reclaimed EMA50 4h — review")

            # (5) strong short-term impulse
            if abs(f.ret_1h) >= CFG.SIGNAL_MOMENTUM_1H:
                self._fire(sym, "impulse", f"ret_1h {f.ret_1h:+.1%}")

    # ---- macro regime (F&G zone change, BTC EMA50 flip) ----
    def _scan_macro(self) -> None:
        if not CFG.SIGNAL_MACRO_ENABLED:
            return
        try:
            fg = data.get_fear_greed()
            btc = data.compute_features("BTCUSDT")
        except Exception as e:
            log.debug(f"scanner macro: {e}")
            return
        fg_class = fg.get("classification")
        if (self._prev_fg_class is not None and fg_class != self._prev_fg_class
                and fg_class != "unknown"):
            self._fire(None, "macro_fear_greed", f"F&G {self._prev_fg_class}→{fg_class}")
        self._prev_fg_class = fg_class

        if (self._prev_btc_above_ema50_4h is not None
                and btc.above_ema50_4h != self._prev_btc_above_ema50_4h):
            state = "reclaimed" if btc.above_ema50_4h else "lost"
            self._fire(None, "macro_btc_ema50", f"BTC {state} EMA50 4h")
        self._prev_btc_above_ema50_4h = btc.above_ema50_4h

    # ---- news (CryptoPanic; requires a token, auto-disabled otherwise) ----
    def _scan_news(self) -> None:
        if not CFG.NEWS_TRIGGER_ENABLED:
            return
        if not CFG.CRYPTOPANIC_TOKEN:
            if not self._news_disabled_logged:
                log.info("news trigger disabled — set CRYPTOPANIC_TOKEN in .env to enable")
                self._news_disabled_logged = True
            return
        now = time.monotonic()
        if now - self._last_news_poll < CFG.NEWS_POLL_SECONDS:
            return
        self._last_news_poll = now

        watch = self.market_state.watch()
        bases = {s.replace("USDT", "") for s in watch}
        try:
            headlines = data.get_news_headlines(sorted(watch))
        except Exception as e:
            log.debug(f"scanner news: {e}")
            return
        for h in headlines:
            title = h.get("title", "")
            key = h.get("url") or title
            if not key or key in self._seen_news:
                continue
            self._seen_news.add(key)
            currencies = [c for c in (h.get("currencies") or []) if c in bases]
            if not currencies:
                continue
            sym = currencies[0] + "USDT"
            self._fire(sym, "news", title[:120])

        # keep the seen-set bounded
        if len(self._seen_news) > 500:
            self._seen_news = set(list(self._seen_news)[-250:])

    # ---- thread body ----
    def run(self) -> None:
        log.info(f"signal scanner online (every {CFG.SCANNER_INTERVAL_SECONDS}s, free — no API cost)")
        # First pass establishes baselines silently, then loop.
        while not self.stop_event.is_set():
            start = time.monotonic()
            try:
                self._scan_technicals()
                self._scan_macro()
                self._scan_news()
            except Exception as e:
                log.error(f"scanner iteration failed: {e}")
                journal.log_event("ERROR", f"scanner: {e}")
            elapsed = time.monotonic() - start
            self.stop_event.wait(max(1.0, CFG.SCANNER_INTERVAL_SECONDS - elapsed))
