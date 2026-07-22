"""Unit tests for the anti-churn entry guards and the drawdown brake."""
import dataclasses
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from config import CFG
import guards
import journal


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class _DBTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        cfg = dataclasses.replace(CFG, JOURNAL_DB=Path(self.tmpdir) / "journal.db")
        self._p = patch.object(journal, "CFG", cfg)
        self._p.start()
        journal.init()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_trade(self, symbol, kind, hours_ago=0.0):
        with journal.db() as c:
            c.execute(
                "INSERT INTO trades (ts, symbol, side, qty, price, notional_usdt, leverage, kind) "
                "VALUES (?, ?, 'LONG', 1, 1, 10, 10, ?)",
                (_iso(hours_ago), symbol, kind),
            )


class TestReentryCooldown(_DBTest):
    def test_denied_right_after_stop(self):
        self._insert_trade("XUSDT", "sl", hours_ago=0.5)
        g = guards.EntryGuard()
        deny = g.check("XUSDT", n_open=5)
        self.assertIsNotNone(deny)
        self.assertIn("cooldown", deny)

    def test_allowed_after_cooldown(self):
        self._insert_trade("XUSDT", "sl",
                           hours_ago=CFG.REENTRY_COOLDOWN_HOURS_AFTER_STOP + 1)
        self.assertIsNone(guards.EntryGuard().check("XUSDT", n_open=5))

    def test_liq_guard_also_triggers_cooldown(self):
        self._insert_trade("XUSDT", "liq_guard", hours_ago=1.0)
        self.assertIsNotNone(guards.EntryGuard().check("XUSDT", n_open=5))

    def test_take_profit_does_not_trigger_cooldown(self):
        # A winner re-qualifying is fine — only LOSING exits are churn.
        self._insert_trade("XUSDT", "tp", hours_ago=0.2)
        self.assertIsNone(guards.EntryGuard().check("XUSDT", n_open=5))


class TestDailyBudgets(_DBTest):
    def test_symbol_daily_cap(self):
        for _ in range(CFG.MAX_OPENS_PER_SYMBOL_PER_DAY):
            self._insert_trade("YUSDT", "open", hours_ago=0.1)
        deny = guards.EntryGuard().check("YUSDT", n_open=5)
        self.assertIsNotNone(deny)
        self.assertIn("today", deny)

    def test_global_daily_cap(self):
        for i in range(CFG.MAX_OPENS_PER_DAY):
            self._insert_trade(f"S{i}USDT", "open", hours_ago=0.1)
        deny = guards.EntryGuard().check("NEWUSDT", n_open=5)
        self.assertIsNotNone(deny)
        self.assertIn("budget", deny)

    def test_under_caps_allowed(self):
        self._insert_trade("YUSDT", "open", hours_ago=0.1)
        self.assertIsNone(guards.EntryGuard().check("YUSDT", n_open=5))


class TestDefensiveMode(_DBTest):
    def test_adjust_halves_margin_and_caps_leverage(self):
        g = guards.EntryGuard()
        g.defensive = True
        margin, lev = g.adjust(200.0, 20)
        self.assertAlmostEqual(margin, 200.0 * CFG.DEFENSIVE_MARGIN_FACTOR)
        self.assertEqual(lev, CFG.DEFENSIVE_MAX_LEVERAGE)

    def test_adjust_noop_when_not_defensive(self):
        g = guards.EntryGuard()
        self.assertEqual(g.adjust(200.0, 20), (200.0, 20))

    def test_defensive_blocks_beyond_reduced_minimum(self):
        g = guards.EntryGuard()
        g.defensive = True
        deny = g.check("ZUSDT", n_open=CFG.DEFENSIVE_MIN_POSITIONS)
        self.assertIsNotNone(deny)
        self.assertIn("defensive", deny)
        # Under the reduced minimum entries still go through
        self.assertIsNone(g.check("ZUSDT", n_open=CFG.DEFENSIVE_MIN_POSITIONS - 1))


class TestFailureBlacklist(_DBTest):
    def test_blacklist_after_repeated_failures(self):
        g = guards.EntryGuard()
        for _ in range(CFG.ENTRY_FAIL_BLACKLIST_AFTER):
            g.record_failure("KAITOUSDT")
        deny = g.check("KAITOUSDT", n_open=5)
        self.assertIsNotNone(deny)
        self.assertIn("blacklisted", deny)

    def test_success_resets_failure_count(self):
        g = guards.EntryGuard()
        g.record_failure("KAITOUSDT")
        g.record_success("KAITOUSDT")
        g.record_failure("KAITOUSDT")
        self.assertIsNone(g.check("KAITOUSDT", n_open=5))


class TestRecoveryPool(_DBTest):
    def test_pool_math_loss_adds_multiplied_win_drains(self):
        pool = guards.apply_income_to_pool(0.0, [-100.0])
        self.assertAlmostEqual(pool, 110.0)          # 1.1× the loss
        pool = guards.apply_income_to_pool(pool, [60.0])
        self.assertAlmostEqual(pool, 50.0)           # wins drain 1:1
        pool = guards.apply_income_to_pool(pool, [200.0])
        self.assertEqual(pool, 0.0)                  # floored at zero

    def test_update_from_income_history_with_cursor(self):
        class FC:
            def futures_income_history(self, **k):
                return [
                    {"income": "-50.0", "time": CFG.RESET_TS_MS + 1000},
                    {"income": "20.0", "time": CFG.RESET_TS_MS + 2000},
                ]
        pool = guards.update_recovery_pool(FC())
        self.assertAlmostEqual(pool, 50 * 1.1 - 20)
        # Second sweep: cursor advanced → same rows ignored, pool unchanged
        self.assertAlmostEqual(guards.update_recovery_pool(FC()), 50 * 1.1 - 20)

    def test_allocate_draws_and_deducts(self):
        journal.set_meta("loss_pool", "100")
        extra = guards.allocate_recovery(base_margin=200.0, equity=4000.0)
        self.assertAlmostEqual(extra, 100.0)          # full pool fits under caps
        self.assertAlmostEqual(guards.recovery_pool(), 0.0)

    def test_allocate_capped_by_position_pct(self):
        journal.set_meta("loss_pool", "5000")
        extra = guards.allocate_recovery(base_margin=200.0, equity=1000.0)
        # position cap = 30% of 1000 = 300 → extra ≤ 100
        self.assertAlmostEqual(extra, 100.0)
        self.assertAlmostEqual(guards.recovery_pool(), 4900.0)

    def test_allocate_capped_by_extra_factor(self):
        journal.set_meta("loss_pool", "5000")
        extra = guards.allocate_recovery(base_margin=200.0, equity=100000.0)
        self.assertAlmostEqual(extra, 400.0)          # ≤ 2× base slice

    def test_refund_returns_to_pool(self):
        journal.set_meta("loss_pool", "10")
        guards.refund_recovery(90.0)
        self.assertAlmostEqual(guards.recovery_pool(), 100.0)

    def test_empty_pool_allocates_nothing(self):
        self.assertEqual(guards.allocate_recovery(200.0, 4000.0), 0.0)


class TestDrawdownBrakeDisabled(_DBTest):
    def test_disabled_brake_never_defensive_and_unsticks(self):
        journal.set_meta("defensive_mode", "1")  # stuck from a previous regime
        self.assertFalse(guards.update_drawdown_state(1000.0))  # deep loss, still off
        self.assertEqual(journal.get_meta("defensive_mode"), "0")


class TestDrawdownBrake(_DBTest):
    def setUp(self):
        super().setUp()
        cfg_on = dataclasses.replace(CFG, DRAWDOWN_BRAKE_ENABLED=True)
        self._pg = patch.object(guards, "CFG", cfg_on)
        self._pg.start()

    def tearDown(self):
        self._pg.stop()
        super().tearDown()

    def test_peak_ratchets_and_brake_engages(self):
        self.assertFalse(guards.update_drawdown_state(5000.0))   # sets peak
        self.assertFalse(guards.update_drawdown_state(4800.0))   # -4%: still off
        self.assertTrue(guards.update_drawdown_state(4500.0))    # -10%: ON
        self.assertEqual(journal.get_meta("defensive_mode"), "1")

    def test_hysteresis_release(self):
        guards.update_drawdown_state(5000.0)
        guards.update_drawdown_state(4500.0)                     # ON
        self.assertTrue(guards.update_drawdown_state(4550.0))    # -9%: still ON
        self.assertFalse(guards.update_drawdown_state(4850.0))   # -3%: OFF (past half-gap)

    def test_new_peak_rearms(self):
        guards.update_drawdown_state(5000.0)
        guards.update_drawdown_state(4500.0)                     # ON
        self.assertFalse(guards.update_drawdown_state(5200.0))   # new peak, OFF
        self.assertEqual(float(journal.get_meta("equity_peak")), 5200.0)


if __name__ == "__main__":
    unittest.main()
