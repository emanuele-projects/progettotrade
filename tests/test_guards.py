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


class TestDrawdownBrake(_DBTest):
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
