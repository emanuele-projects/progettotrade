"""Unit tests for the long-term memory (memory.py + journal lesson/meta store).

DB-backed tests run against a throwaway sqlite file by patching journal.CFG with
a dataclasses.replace() copy (Config is frozen, so we rebind the module name
rather than mutate the instance)."""
import dataclasses
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import CFG
import journal
import memory


def _now_ms() -> int:
    return int(time.time() * 1000)


class FakeClient:
    """Minimal stand-in for the binance client used by memory.symbol_records."""
    def __init__(self, income_rows):
        self._rows = income_rows

    def futures_income_history(self, **kwargs):
        return self._rows


class _DBTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db = Path(self.tmpdir) / "journal.db"
        self.cfg = dataclasses.replace(CFG, JOURNAL_DB=db)
        self._patchers = [patch.object(journal, "CFG", self.cfg)]
        for p in self._patchers:
            p.start()
        journal.init()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestLessonStore(_DBTest):
    def test_replace_and_read_back(self):
        n = journal.replace_lessons([
            {"scope": "global", "text": "Cut leverage on high-ATR movers"},
            {"scope": "DOGEUSDT", "text": "Stop shorting DOGE, it squeezes"},
        ])
        self.assertEqual(n, 2)
        active = journal.get_active_lessons()
        self.assertEqual(len(active), 2)
        texts = {l["text"] for l in active}
        self.assertIn("Stop shorting DOGE, it squeezes", texts)

    def test_replace_supersedes_previous_set(self):
        journal.replace_lessons([{"scope": "global", "text": "old lesson"}])
        journal.replace_lessons([{"scope": "global", "text": "new lesson"}])
        active = journal.get_active_lessons()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["text"], "new lesson")

    def test_empty_replace_is_noop(self):
        journal.replace_lessons([{"scope": "global", "text": "keep me"}])
        # An empty/failed reflection must NOT wipe existing memory.
        n = journal.replace_lessons([])
        self.assertEqual(n, 0)
        self.assertEqual(len(journal.get_active_lessons()), 1)

    def test_blank_text_filtered(self):
        n = journal.replace_lessons([
            {"scope": "global", "text": "   "},
            {"scope": "global", "text": "real"},
        ])
        self.assertEqual(n, 1)

    def test_deactivate(self):
        lid = journal.add_lesson("temp", scope="global")
        self.assertEqual(len(journal.get_active_lessons()), 1)
        journal.deactivate_lesson(lid)
        self.assertEqual(len(journal.get_active_lessons()), 0)


class TestMetaStore(_DBTest):
    def test_roundtrip_and_upsert(self):
        self.assertIsNone(journal.get_meta("k"))
        self.assertEqual(journal.get_meta("k", "fallback"), "fallback")
        journal.set_meta("k", "v1")
        self.assertEqual(journal.get_meta("k"), "v1")
        journal.set_meta("k", "v2")
        self.assertEqual(journal.get_meta("k"), "v2")

    def test_seconds_since_last_reflection(self):
        with patch.object(memory, "journal", journal):
            self.assertIsNone(memory.seconds_since_last_reflection())
            from datetime import datetime, timezone
            journal.set_meta("last_reflection_ts", datetime.now(timezone.utc).isoformat())
            age = memory.seconds_since_last_reflection()
            self.assertIsNotNone(age)
            self.assertLess(age, 5)


class TestSymbolRecords(unittest.TestCase):
    def test_aggregation_wins_losses_net(self):
        now = _now_ms()
        rows = [
            {"symbol": "SOLUSDT", "income": "10.0", "time": now},
            {"symbol": "SOLUSDT", "income": "-4.0", "time": now},
            {"symbol": "SOLUSDT", "income": "6.0", "time": now},
            {"symbol": "DOGEUSDT", "income": "-3.0", "time": now},
            {"symbol": "DOGEUSDT", "income": "-5.0", "time": now},
        ]
        agg = memory.symbol_records(FakeClient(rows), lookback_days=30)
        self.assertEqual(agg["SOLUSDT"]["wins"], 2)
        self.assertEqual(agg["SOLUSDT"]["losses"], 1)
        self.assertAlmostEqual(agg["SOLUSDT"]["net"], 12.0)
        self.assertAlmostEqual(agg["DOGEUSDT"]["net"], -8.0)

    def test_lookback_excludes_old_rows(self):
        old = _now_ms() - 40 * 86400 * 1000  # 40 days ago
        rows = [{"symbol": "BTCUSDT", "income": "100.0", "time": old}]
        agg = memory.symbol_records(FakeClient(rows), lookback_days=30)
        self.assertNotIn("BTCUSDT", agg)

    def test_format_tags_working_and_burning(self):
        agg = {
            "SOLUSDT": {"wins": 3, "losses": 1, "net": 20.0, "n": 4},
            "DOGEUSDT": {"wins": 0, "losses": 3, "net": -9.0, "n": 3},
        }
        lines = memory._format_symbol_records(agg, top_n=10)
        joined = "\n".join(lines)
        self.assertIn("SOL", joined)
        self.assertIn("WORKING", joined)
        self.assertIn("BURNING", joined)
        # most-traded first
        self.assertTrue(lines[0].startswith("SOL"))

    def test_format_only_subset(self):
        agg = {
            "SOLUSDT": {"wins": 3, "losses": 1, "net": 20.0, "n": 4},
            "DOGEUSDT": {"wins": 0, "losses": 3, "net": -9.0, "n": 3},
        }
        lines = memory._format_symbol_records(agg, top_n=10, only={"DOGEUSDT"})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("DOGE"))


class TestBuildMemoryBlock(_DBTest):
    def test_none_when_empty(self):
        block = memory.build_memory_block(FakeClient([]))
        self.assertIsNone(block)

    def test_includes_lessons_and_records(self):
        journal.replace_lessons([{"scope": "global", "text": "widen stops on movers"}])
        now = _now_ms()
        rows = [
            {"symbol": "SOLUSDT", "income": "10.0", "time": now},
            {"symbol": "SOLUSDT", "income": "5.0", "time": now},
        ]
        block = memory.build_memory_block(FakeClient(rows))
        self.assertIsNotNone(block)
        self.assertIn("YOUR MEMORY", block)
        self.assertIn("widen stops on movers", block)
        self.assertIn("SOL", block)

    def test_lessons_only_skips_symbol_table(self):
        journal.replace_lessons([{"scope": "global", "text": "a lesson"}])
        rows = [{"symbol": "SOLUSDT", "income": "10.0", "time": _now_ms()}]
        block = memory.build_memory_block(FakeClient(rows), lessons_only=True)
        self.assertIn("a lesson", block)
        self.assertNotIn("track record", block)


if __name__ == "__main__":
    unittest.main()
