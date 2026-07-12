"""Trigger plumbing for the event-driven agent.

MarketStream / RiskEngine produce Triggers onto the TriggerBus; the main loop
consumes them and asks TriggerPolicy whether a batch justifies a Claude call.
"""
from __future__ import annotations
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from config import CFG


TriggerKind = Literal[
    "baseline", "risk_exit", "price_move", "funding_flip", "pnl_threshold", "manual",
    "signal", "news", "macro",
]


@dataclass
class Trigger:
    kind: TriggerKind
    symbol: str | None = None
    detail: str = ""
    ts: float = field(default_factory=time.time)

    def tag(self) -> str:
        """Compact form stored in journal columns, e.g. 'event:price_move:BTCUSDT'."""
        base = f"event:{self.kind}"
        return f"{base}:{self.symbol}" if self.symbol else base


class TriggerBus:
    """Thin thread-safe funnel: many producers, one consumer (the main loop)."""

    def __init__(self, maxsize: int = 1000):
        self._q: queue.Queue[Trigger] = queue.Queue(maxsize=maxsize)

    def emit(self, trigger: Trigger) -> bool:
        try:
            self._q.put_nowait(trigger)
            return True
        except queue.Full:
            return False  # bus flooded — drop; baseline cycle will catch up

    def get_or_none(self, timeout: float) -> Trigger | None:
        try:
            return self._q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def drain(self) -> list[Trigger]:
        out: list[Trigger] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out


class TriggerPolicy:
    """Decides whether a trigger batch becomes a Claude call.

    Rules: minimum interval between ANY two Claude calls, hourly token bucket
    for event calls, risk_exit exempt from debounce (but not from the bucket).
    The debounce wait itself lives in the main loop; this object is the
    bookkeeping."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_call_monotonic: float = -1e9
        self._event_calls: deque[float] = deque()  # monotonic timestamps, last hour

    def seconds_since_last_call(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_call_monotonic

    def _prune(self, now: float) -> None:
        while self._event_calls and now - self._event_calls[0] > 3600:
            self._event_calls.popleft()

    def can_event_call(self) -> tuple[bool, str]:
        """(allowed, reason_if_denied) for an event-triggered Claude call."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_call_monotonic < CFG.EVENT_MIN_CALL_INTERVAL_SECONDS:
                wait = CFG.EVENT_MIN_CALL_INTERVAL_SECONDS - (now - self._last_call_monotonic)
                return False, f"min-interval ({wait:.0f}s left)"
            self._prune(now)
            if len(self._event_calls) >= CFG.EVENT_MAX_CALLS_PER_HOUR:
                return False, f"hourly cap {CFG.EVENT_MAX_CALLS_PER_HOUR} reached"
            return True, ""

    def record_call(self, is_event: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_call_monotonic = now
            if is_event:
                self._prune(now)
                self._event_calls.append(now)

    def baseline_should_skip(self) -> bool:
        """Skip the baseline cycle if any Claude call ran very recently."""
        return self.seconds_since_last_call() < CFG.BASELINE_SKIP_IF_CALLED_WITHIN
