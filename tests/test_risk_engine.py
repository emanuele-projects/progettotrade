"""Unit tests for risk_engine pure logic (no network, no threads)."""
import unittest

from risk_engine import PositionCache, CachedPosition, liq_guard_price, crossed_guard


class TestLiqGuard(unittest.TestCase):
    def test_guard_price_long(self):
        # LONG: entry 100, liq 95 (5% away), fraction 0.75 → guard at 96.25
        self.assertAlmostEqual(liq_guard_price(100.0, 95.0, 0.75), 96.25)

    def test_guard_price_short(self):
        # SHORT: entry 100, liq 105, fraction 0.75 → guard at 103.75
        self.assertAlmostEqual(liq_guard_price(100.0, 105.0, 0.75), 103.75)

    def test_crossed_long(self):
        self.assertTrue(crossed_guard("LONG", 96.0, 96.25))    # below guard → close
        self.assertFalse(crossed_guard("LONG", 97.0, 96.25))   # above guard → safe

    def test_crossed_short(self):
        self.assertTrue(crossed_guard("SHORT", 104.0, 103.75))  # above guard → close
        self.assertFalse(crossed_guard("SHORT", 103.0, 103.75)) # below guard → safe


def _cached(symbol="XRPUSDT", side="LONG", qty=100.0, entry=1.0,
            margin=20.0, lev=5) -> CachedPosition:
    return CachedPosition(symbol=symbol, side=side, qty=qty, entry_price=entry,
                          isolated_margin=margin, leverage=lev,
                          sl_pct=-0.20, tp_pct=0.30, liquidation_price=0.0)


class TestPositionCacheAccountUpdate(unittest.TestCase):
    def _seeded_cache(self) -> PositionCache:
        cache = PositionCache()
        cache._positions["XRPUSDT"] = _cached()
        return cache

    def test_update_qty_entry_margin(self):
        cache = self._seeded_cache()
        cache.apply_account_update({"P": [
            {"s": "XRPUSDT", "pa": "150", "ep": "1.05", "iw": "30.0"},
        ]})
        p = cache.get("XRPUSDT")
        self.assertEqual(p.qty, 150.0)
        self.assertEqual(p.side, "LONG")
        self.assertAlmostEqual(p.entry_price, 1.05)
        self.assertAlmostEqual(p.isolated_margin, 30.0)

    def test_zero_amount_removes(self):
        cache = self._seeded_cache()
        cache.apply_account_update({"P": [{"s": "XRPUSDT", "pa": "0"}]})
        self.assertIsNone(cache.get("XRPUSDT"))

    def test_negative_amount_flips_side(self):
        cache = self._seeded_cache()
        cache.apply_account_update({"P": [{"s": "XRPUSDT", "pa": "-80", "ep": "1.02"}]})
        p = cache.get("XRPUSDT")
        self.assertEqual(p.side, "SHORT")
        self.assertEqual(p.qty, 80.0)

    def test_unknown_symbol_flags_reconcile(self):
        cache = self._seeded_cache()
        self.assertFalse(cache.needs_reconcile.is_set())
        cache.apply_account_update({"P": [{"s": "BTCUSDT", "pa": "0.5", "ep": "60000"}]})
        self.assertTrue(cache.needs_reconcile.is_set())
        self.assertIsNone(cache.get("BTCUSDT"))  # not invented — reconcile will add it

    def test_malformed_entries_ignored(self):
        cache = self._seeded_cache()
        cache.apply_account_update({"P": [{"pa": "10"}, {"s": "XRPUSDT"}, {}]})
        self.assertIsNotNone(cache.get("XRPUSDT"))


if __name__ == "__main__":
    unittest.main()
