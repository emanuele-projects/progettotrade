"""Unit tests for the signal scanner logic (mocked features, no network)."""
import unittest
from unittest import mock

from data import Features
from events import TriggerBus
from risk_engine import PositionCache, CachedPosition
from scanner import SignalScanner
from stream import MarketState


def _features(symbol="SOLUSDT", *, above_ema50_4h=True, rsi_4h=55.0,
              dist_high=-0.10, dist_low=0.20, ret_1h=0.005) -> Features:
    return Features(
        symbol=symbol, risk_tier="mid_cap", last_price=100.0,
        ret_1h=ret_1h, ret_24h=0.0, ret_7d=0.0, rsi_14=50.0,
        ema20=99.0, ema50=98.0, above_ema50=True, volume_24h_usd=1e8,
        ret_4h=0.0, ret_1d=0.0, above_ema50_4h=above_ema50_4h,
        above_ema50_1d=True, rsi_4h=rsi_4h, atr_pct_24h=0.03,
        dist_from_high_30d=dist_high, dist_from_low_30d=dist_low,
        funding_rate_8h=0.0001, open_interest_change_24h=0.0,
        top_trader_long_pct=0.55, max_leverage=20,
    )


class _Harness:
    """Drives the scanner one symbol at a time with a scripted feature sequence."""
    def __init__(self, symbol="SOLUSDT", position: CachedPosition | None = None):
        self.symbol = symbol
        self.ms = MarketState()
        self.ms.set_watch({symbol})
        self.pc = PositionCache()
        if position is not None:
            self.pc._positions[symbol] = position
        self.bus = TriggerBus()
        self.scanner = SignalScanner(self.ms, self.pc, self.bus)

    def scan_with(self, feats: Features) -> list:
        with mock.patch("data.compute_features", return_value=feats):
            self.scanner._scan_technicals()
        return self.bus.drain()


class TestScannerTransitions(unittest.TestCase):
    def test_first_scan_is_silent(self):
        h = _Harness()
        fired = h.scan_with(_features(above_ema50_4h=True))
        self.assertEqual(fired, [], "first sighting must only establish a baseline")

    def test_ema_cross_up_fires_once(self):
        h = _Harness()
        h.scan_with(_features(above_ema50_4h=False))          # baseline: below
        fired = h.scan_with(_features(above_ema50_4h=True))   # transition up
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].kind, "signal")
        self.assertIn("ema_cross_up", fired[0].detail)
        # persistent condition must NOT re-fire (debounce)
        again = h.scan_with(_features(above_ema50_4h=True))
        self.assertEqual(again, [])

    def test_ema_cross_down_fires(self):
        h = _Harness()
        h.scan_with(_features(above_ema50_4h=True))
        fired = h.scan_with(_features(above_ema50_4h=False))
        self.assertTrue(any("ema_cross_down" in t.detail for t in fired))

    def test_rsi_exit_oversold(self):
        h = _Harness()
        h.scan_with(_features(rsi_4h=25.0))                   # oversold
        fired = h.scan_with(_features(rsi_4h=35.0))           # exits upward
        self.assertTrue(any("rsi_exit_oversold" in t.detail for t in fired))

    def test_rsi_exit_overbought(self):
        h = _Harness()
        h.scan_with(_features(rsi_4h=75.0))
        fired = h.scan_with(_features(rsi_4h=65.0))
        self.assertTrue(any("rsi_exit_overbought" in t.detail for t in fired))

    def test_breakout_high(self):
        h = _Harness()
        h.scan_with(_features(dist_high=-0.05))               # 5% below 30d high
        fired = h.scan_with(_features(dist_high=-0.005))      # crossed into top 1%
        self.assertTrue(any("breakout_high" in t.detail for t in fired))

    def test_impulse_fires_on_big_1h_move(self):
        h = _Harness()
        h.scan_with(_features(ret_1h=0.0))
        fired = h.scan_with(_features(ret_1h=0.05))           # +5% in 1h
        self.assertTrue(any("impulse" in t.detail for t in fired))

    def test_position_thesis_break_long(self):
        pos = CachedPosition(symbol="SOLUSDT", side="LONG", qty=1.0, entry_price=100.0,
                             isolated_margin=20.0, leverage=10, sl_pct=-0.2, tp_pct=0.3,
                             liquidation_price=90.0)
        h = _Harness(position=pos)
        h.scan_with(_features(above_ema50_4h=True))
        fired = h.scan_with(_features(above_ema50_4h=False))
        self.assertTrue(any("thesis_break" in t.detail for t in fired),
                        "a held LONG losing EMA50 4h must raise a review signal")

    def test_flat_market_stays_silent(self):
        h = _Harness()
        base = _features(above_ema50_4h=True, rsi_4h=55.0, dist_high=-0.10, ret_1h=0.005)
        h.scan_with(base)
        # three more identical scans — nothing changed → no Claude wakeups
        for _ in range(3):
            self.assertEqual(h.scan_with(base), [])


if __name__ == "__main__":
    unittest.main()
