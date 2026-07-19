"""Unit tests for the always-invested portfolio mode (2026-07-15):
- server_protected positions: risk engine skips SL/TP, keeps liq-guard
- build_portfolio_status mandate text
- protective orders active on testnet (gate open)
"""
import queue
import unittest
from unittest.mock import patch

from config import CFG
import strategy
from risk_engine import CachedPosition, PositionCache, RiskEngine
from stream import MarketState
from events import TriggerBus


def _cached(symbol="XRPUSDT", side="LONG", qty=100.0, entry=1.0,
            margin=20.0, lev=5, server_protected=False) -> CachedPosition:
    return CachedPosition(symbol=symbol, side=side, qty=qty, entry_price=entry,
                          isolated_margin=margin, leverage=lev,
                          sl_pct=-0.20, tp_pct=0.30, liquidation_price=0.0,
                          server_protected=server_protected)


def _engine_with(cached: CachedPosition, mark: float) -> tuple[RiskEngine, list]:
    cache = PositionCache()
    cache._positions[cached.symbol] = cached
    state = MarketState()
    state.set_watch({cached.symbol})
    state.update(cached.symbol, mark, 0.0, 0)
    engine = RiskEngine(cache, state, queue.Queue(), TriggerBus(), initial_wallet=1000.0)
    closes: list = []
    engine._close = lambda c, m, reason, detail: closes.append(reason)  # capture
    return engine, closes


class TestServerProtectedBackstop(unittest.TestCase):
    def test_engine_enforces_sl_when_not_protected(self):
        # LONG 5x, entry 1.0, margin 20, qty 100 → mark 0.955 = roe -22.5% ≤ -20%
        engine, closes = _engine_with(_cached(server_protected=False), mark=0.955)
        engine._check("XRPUSDT")
        self.assertEqual(closes, ["sl"])

    def test_engine_skips_sl_when_server_protected(self):
        engine, closes = _engine_with(_cached(server_protected=True), mark=0.955)
        engine._check("XRPUSDT")
        self.assertEqual(closes, [])  # the exchange-held STOP_MARKET owns this exit

    def test_engine_skips_tp_when_server_protected(self):
        # mark 1.07 → roe +35% ≥ +30%
        engine, closes = _engine_with(_cached(server_protected=True), mark=1.07)
        engine._check("XRPUSDT")
        self.assertEqual(closes, [])

    def test_liq_guard_still_fires_when_server_protected(self):
        # liq price known: entry 1.0, liq 0.81 → guard at 1 - 0.75*0.19 = 0.8575
        cached = _cached(server_protected=True)
        cached.liquidation_price = 0.81
        engine, closes = _engine_with(cached, mark=0.85)
        engine._check("XRPUSDT")
        self.assertEqual(closes, ["liq_guard"])  # disaster insurance never delegates


class TestPortfolioStatus(unittest.TestCase):
    def test_below_minimum_mandates_entries(self):
        n = CFG.MIN_OPEN_POSITIONS - 1
        txt = strategy.build_portfolio_status(n)
        need = CFG.MIN_OPEN_POSITIONS - n
        self.assertIn("BELOW the minimum", txt)
        self.assertIn(f"at least {need} new position(s)", txt)

    def test_at_minimum_is_compliant(self):
        txt = strategy.build_portfolio_status(CFG.MIN_OPEN_POSITIONS)
        self.assertIn("book compliant", txt)

    def test_above_cap_prunes(self):
        txt = strategy.build_portfolio_status(CFG.MAX_CONCURRENT_POSITIONS + 3)
        self.assertIn("ABOVE THE CONCENTRATED CAP", txt)
        self.assertIn("CLOSE", txt)

    def test_defensive_mode_text(self):
        txt = strategy.build_portfolio_status(3, defensive=True)
        self.assertIn("DEFENSIVE MODE", txt)

    def test_status_flows_into_user_prompt(self):
        status = strategy.build_portfolio_status(CFG.MIN_OPEN_POSITIONS - 1)
        prompt = strategy.build_user_prompt(
            candidates=[], open_positions=[],
            fear_greed={"value": 30, "classification": "Fear"},
            btc_features=_btc(), news=[], portfolio_status=status,
        )
        self.assertIn("=== PORTFOLIO STATUS ===", prompt)
        self.assertIn("BELOW the minimum", prompt)


def _btc():
    from data import Features
    return Features(
        symbol="BTCUSDT", risk_tier="large_cap", last_price=64000.0,
        ret_1h=0.0, ret_24h=0.0, ret_7d=0.0, rsi_14=50.0,
        ema20=64000.0, ema50=63000.0, above_ema50=True,
        volume_24h_usd=1e9, ret_4h=0.0, ret_1d=0.0, rsi_4h=50.0,
        above_ema50_4h=True, above_ema50_1d=True, atr_pct_24h=0.02,
        dist_from_high_30d=-0.05, dist_from_low_30d=0.10,
        funding_rate_8h=0.0001, open_interest_change_24h=0.0,
        top_trader_long_pct=0.55, max_leverage=20,
    )


class TestStrictToolSchema(unittest.TestCase):
    """Opus omits non-required fields: the tool MUST stay strict with every
    field required, or entries silently fall back to default stops (live bug
    2026-07-15: all entries -30%/+10%/10x while reasoning said otherwise)."""

    def test_strict_and_all_fields_required(self):
        self.assertTrue(strategy.SUBMIT_TOOL.get("strict"))
        items = strategy.SUBMIT_TOOL["input_schema"]["properties"]["decisions"]["items"]
        self.assertEqual(
            set(items["required"]),
            {"symbol", "action", "confidence", "reasoning",
             "stop_loss_pct", "take_profit_pct", "leverage"},
        )
        self.assertFalse(items["additionalProperties"])

    def test_no_numeric_minmax_in_strict_schema(self):
        # strict mode rejects minimum/maximum — ranges are enforced by _coerce_decision
        import json
        blob = json.dumps(strategy.SUBMIT_TOOL["input_schema"])
        self.assertNotIn('"minimum"', blob)
        self.assertNotIn('"maximum"', blob)

    def test_coerce_drops_placeholders_on_flat(self):
        d = strategy._coerce_decision({
            "symbol": "X", "action": "flat", "confidence": 0.5, "reasoning": "r",
            "stop_loss_pct": -0.30, "take_profit_pct": 0.10, "leverage": 5,
        })
        self.assertNotIn("stop_loss_pct", d)  # placeholders ignored for non-entries
        self.assertNotIn("leverage", d)


class TestProtectiveOrdersOnTestnet(unittest.TestCase):
    def test_gate_open_on_testnet(self):
        """With SERVER_SIDE_PROTECTION_ON_TESTNET=True the testnet early-return
        is gone: both conditional orders reach the client."""
        import execution

        calls: list[dict] = []

        class FakeClient:
            def futures_create_order(self, **kw):
                calls.append(kw)
                return {}

        with patch.object(execution, "_symbol_filters", return_value=(0.001, 0.01)):
            execution.place_protective_orders(
                FakeClient(), "XRPUSDT", entry_price=1.0, side="LONG",
                sl_pct=-0.20, tp_pct=0.30, leverage=10,
            )
        types = sorted(c["type"] for c in calls)
        self.assertEqual(types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        for c in calls:
            self.assertTrue(c["closePosition"])
            self.assertEqual(c["side"], "SELL")  # LONG closes with SELL
        # ROE→price translation: SL -20% @10x = -2% price; TP +30% @10x = +3%
        sl = next(c for c in calls if c["type"] == "STOP_MARKET")
        tp = next(c for c in calls if c["type"] == "TAKE_PROFIT_MARKET")
        self.assertAlmostEqual(sl["stopPrice"], 0.98, places=6)
        self.assertAlmostEqual(tp["stopPrice"], 1.03, places=6)


if __name__ == "__main__":
    unittest.main()
