"""Unit tests for the pure risk math in execution.py (no network, no db writes)."""
import unittest

from execution import clamp_sl_to_liquidation, estimated_liq_distance, _floor_step
from config import CFG


class TestLiquidationMath(unittest.TestCase):
    def test_estimated_liq_distance(self):
        # liq ≈ 1/L − MMR (0.005)
        self.assertAlmostEqual(estimated_liq_distance(5), 0.195)
        self.assertAlmostEqual(estimated_liq_distance(10), 0.095)
        self.assertAlmostEqual(estimated_liq_distance(15), 1 / 15 - 0.005)
        self.assertAlmostEqual(estimated_liq_distance(20), 0.045)

    def test_liq_distance_never_zero(self):
        self.assertGreater(estimated_liq_distance(200), 0)

    def test_schema_range_never_clamped(self):
        # The Claude schema allows sl ∈ [-0.50, -0.05]. With MMR=0.5% and the
        # 60% fraction, the full range must survive at every allowed leverage —
        # the clamp is a safety net, not an active constraint.
        for lev in CFG.ALLOWED_LEVERAGES:
            for sl in (-0.05, -0.20, -0.35, -0.50):
                self.assertAlmostEqual(clamp_sl_to_liquidation(sl, lev), sl,
                                       msg=f"lev={lev} sl={sl}")

    def test_out_of_range_sl_gets_clamped(self):
        # At 20x: liq dist 4.5%, max SL price distance 60% → 2.7% → max ROE −54%.
        clamped = clamp_sl_to_liquidation(-0.80, 20)
        self.assertAlmostEqual(clamped, -0.54)
        # At 10x: liq 9.5%, max dist 5.7% → max ROE −57%.
        self.assertAlmostEqual(clamp_sl_to_liquidation(-0.80, 10), -0.57)

    def test_clamped_sl_stays_inside_liquidation(self):
        for lev in CFG.ALLOWED_LEVERAGES:
            clamped = clamp_sl_to_liquidation(-5.0, lev)  # absurd input
            price_dist = abs(clamped) / lev
            self.assertLess(price_dist, estimated_liq_distance(lev),
                            msg=f"lev={lev}: SL price distance must stay inside liq")

    def test_clamp_preserves_sign(self):
        self.assertLess(clamp_sl_to_liquidation(-0.99, 20), 0)


class TestFloorStep(unittest.TestCase):
    def test_floor_step_no_float_noise(self):
        self.assertEqual(_floor_step(0.123456, 0.001), 0.123)
        self.assertEqual(_floor_step(1.0, 0.001), 1.0)
        self.assertEqual(_floor_step(2.675, 0.01), 2.67)

    def test_floor_step_lot_sizing(self):
        # $500 margin × 10x at $61234.5 → qty floored to 3 decimals
        qty = 500 * 10 / 61234.5
        self.assertEqual(_floor_step(qty, 0.001), 0.081)


if __name__ == "__main__":
    unittest.main()
