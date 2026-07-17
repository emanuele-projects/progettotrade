"""Unit tests for TriggerPolicy and prompt building (no network)."""
import time
import unittest
from unittest import mock

from config import CFG
from data import Features
from events import Trigger, TriggerBus, TriggerPolicy
import strategy


def _features(symbol="BTCUSDT", max_lev=20) -> Features:
    return Features(
        symbol=symbol, risk_tier="large_cap", last_price=100.0,
        ret_1h=0.01, ret_24h=0.02, ret_7d=0.05, rsi_14=55.0,
        ema20=99.0, ema50=98.0, above_ema50=True, volume_24h_usd=1e9,
        ret_4h=0.01, ret_1d=0.02, above_ema50_4h=True, above_ema50_1d=True,
        rsi_4h=56.0, atr_pct_24h=0.02, dist_from_high_30d=-0.05,
        dist_from_low_30d=0.20, funding_rate_8h=0.0001,
        open_interest_change_24h=0.03, top_trader_long_pct=0.6,
        max_leverage=max_lev,
    )


class TestTriggerPolicy(unittest.TestCase):
    def test_min_interval_blocks(self):
        p = TriggerPolicy()
        p.record_call(is_event=True)
        allowed, reason = p.can_event_call()
        self.assertFalse(allowed)
        self.assertIn("min-interval", reason)

    def test_hourly_cap_blocks(self):
        p = TriggerPolicy()
        base = time.monotonic()
        # simulate EVENT_MAX_CALLS_PER_HOUR calls spread out (older than min-interval)
        p._event_calls.extend(
            base - 3000 + i for i in range(CFG.EVENT_MAX_CALLS_PER_HOUR)
        )
        p._last_call_monotonic = base - CFG.EVENT_MIN_CALL_INTERVAL_SECONDS - 1
        allowed, reason = p.can_event_call()
        self.assertFalse(allowed)
        self.assertIn("hourly cap", reason)

    def test_allowed_after_interval(self):
        p = TriggerPolicy()
        p._last_call_monotonic = time.monotonic() - CFG.EVENT_MIN_CALL_INTERVAL_SECONDS - 1
        allowed, _ = p.can_event_call()
        self.assertTrue(allowed)

    def test_baseline_skip(self):
        p = TriggerPolicy()
        p.record_call(is_event=True)
        self.assertTrue(p.baseline_should_skip())

    def test_bus_drain_and_overflow(self):
        bus = TriggerBus(maxsize=2)
        self.assertTrue(bus.emit(Trigger(kind="price_move", symbol="A")))
        self.assertTrue(bus.emit(Trigger(kind="price_move", symbol="B")))
        self.assertFalse(bus.emit(Trigger(kind="price_move", symbol="C")))  # full → dropped
        drained = bus.drain()
        self.assertEqual([t.symbol for t in drained], ["A", "B"])


class TestPromptBuilding(unittest.TestCase):
    def test_trigger_block_and_focused_header(self):
        prompt = strategy.build_user_prompt(
            candidates=[_features("SOLUSDT", max_lev=20)],
            open_positions=[{"symbol": "BTCUSDT", "side": "SHORT", "qty": 0.01,
                             "entry_price": 100.0, "mark_price": 99.0,
                             "unrealized_pnl_pct": 0.05, "martingale_levels": 0,
                             "sl_pct": -0.2, "tp_pct": 0.3, "leverage": 10}],
            fear_greed={"value": 50, "classification": "Neutral"},
            btc_features=_features("BTCUSDT"),
            news=[],
            trigger_lines=["[price_move] SOLUSDT: +2.61% in 15min"],
            focused=True,
        )
        self.assertTrue(prompt.startswith("=== TRIGGER"))
        self.assertIn("[price_move] SOLUSDT: +2.61% in 15min", prompt)
        self.assertIn("FOCUSED call", prompt)
        self.assertIn("max_lev=20x", prompt)
        self.assertIn("side=SHORT", prompt)

    def test_baseline_prompt_has_no_trigger_block(self):
        prompt = strategy.build_user_prompt(
            candidates=[_features()], open_positions=[],
            fear_greed={"value": 50, "classification": "Neutral"},
            btc_features=_features(), news=[],
        )
        self.assertNotIn("=== TRIGGER", prompt)
        self.assertIn("=== CANDIDATES (decide long, short or flat for each) ===", prompt)

    def test_system_prompt_is_static(self):
        # byte-stability is what keeps the 1h cache valid
        self.assertNotIn("{", strategy.SYSTEM_PROMPT.replace("{'", ""))
        self.assertGreater(len(strategy.SYSTEM_PROMPT), 3000)

    def test_memory_block_injected_into_user_message(self):
        # Long-term memory must ride in the USER message (never the cached system
        # prompt), so the 1h cache stays valid while memory changes daily.
        block = "=== YOUR MEMORY ===\n- [DOGE] stop shorting DOGE, it squeezes"
        prompt = strategy.build_user_prompt(
            candidates=[_features()], open_positions=[],
            fear_greed={"value": 50, "classification": "Neutral"},
            btc_features=_features(), news=[], memory_block=block,
        )
        self.assertIn("YOUR MEMORY", prompt)
        self.assertIn("stop shorting DOGE", prompt)
        # The dynamic memory CONTENT must never leak into the cached system prompt
        # (the prompt only *describes* the memory block; the data rides the user msg).
        self.assertNotIn("stop shorting DOGE", strategy.SYSTEM_PROMPT)


class TestTolerantParsing(unittest.TestCase):
    def test_negative_take_profit_sign_flip(self):
        # the real failure: TP given as -0.42 (should be +0.42)
        raw = {"market_view": "x", "decisions": [
            {"symbol": "LITUSDT", "action": "long", "confidence": 0.58,
             "reasoning": "ok", "leverage": 5, "stop_loss_pct": -0.28,
             "take_profit_pct": -0.42},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertEqual(len(d.decisions), 1)
        self.assertAlmostEqual(d.decisions[0].take_profit_pct, 0.42)
        self.assertAlmostEqual(d.decisions[0].stop_loss_pct, -0.28)

    def test_positive_stop_loss_corrected(self):
        raw = {"market_view": "x", "decisions": [
            {"symbol": "BTCUSDT", "action": "short", "confidence": 0.6,
             "reasoning": "ok", "leverage": 10, "stop_loss_pct": 0.20,
             "take_profit_pct": 0.30},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertAlmostEqual(d.decisions[0].stop_loss_pct, -0.20)

    def test_out_of_range_clamped(self):
        raw = {"market_view": "x", "decisions": [
            {"symbol": "ETHUSDT", "action": "long", "confidence": 1.5,
             "reasoning": "ok", "leverage": 10, "stop_loss_pct": -0.80,
             "take_profit_pct": 0.90},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertEqual(d.decisions[0].confidence, 1.0)
        self.assertAlmostEqual(d.decisions[0].stop_loss_pct, -0.50)
        self.assertAlmostEqual(d.decisions[0].take_profit_pct, 0.50)

    def test_nonstandard_leverage_snapped(self):
        raw = {"market_view": "x", "decisions": [
            {"symbol": "ETHUSDT", "action": "long", "confidence": 0.6,
             "reasoning": "ok", "leverage": 12, "stop_loss_pct": -0.20,
             "take_profit_pct": 0.30},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertEqual(d.decisions[0].leverage, 10)  # 12 → nearest allowed

    def test_unrecoverable_dropped_batch_survives(self):
        raw = {"market_view": "x", "decisions": [
            {"action": "long"},                                  # no symbol → drop
            {"symbol": "ZZZ", "action": "banana"},               # bad action → drop
            {"symbol": "BTCUSDT", "action": "flat", "confidence": 0.5, "reasoning": "hold"},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertEqual(len(d.decisions), 1)
        self.assertEqual(d.decisions[0].symbol, "BTCUSDT")

    def test_flat_needs_no_sltp(self):
        raw = {"market_view": "x", "decisions": [
            {"symbol": "BTCUSDT", "action": "flat", "confidence": 0.5, "reasoning": "hold"},
        ]}
        d = strategy.parse_decisions(raw)
        self.assertIsNone(d.decisions[0].stop_loss_pct)


if __name__ == "__main__":
    unittest.main()
