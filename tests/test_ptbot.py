"""Unit tests for the runner's pure helpers. No engine, no browser, stdlib only.

Run with:  python3 -m pytest tests   (or python3 -m unittest discover tests)
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ptbot", ROOT / "scripts" / "ptbot.py")
ptbot = importlib.util.module_from_spec(spec)
sys.modules["ptbot"] = ptbot
spec.loader.exec_module(ptbot)

LA = ZoneInfo("America/Los_Angeles")


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 13, hour, minute, second, tzinfo=LA)


class PlanWaitTests(unittest.TestCase):
    def test_on_time_routine_waits_until_lead_before_release(self):
        plan = ptbot.plan_wait(at(11, 15), at(12, 0), lead_seconds=180)
        self.assertEqual(plan.action, "wait")
        self.assertEqual(plan.fire_at, at(11, 57))
        self.assertAlmostEqual(plan.wait_seconds, 42 * 60)
        self.assertFalse(plan.late_for_release)

    def test_late_routine_inside_lead_runs_now_and_is_not_flagged(self):
        plan = ptbot.plan_wait(at(11, 58), at(12, 0), lead_seconds=180)
        self.assertEqual(plan.action, "run-now")
        self.assertFalse(plan.late_for_release)

    def test_routine_after_release_runs_now_and_is_flagged(self):
        plan = ptbot.plan_wait(at(12, 7), at(12, 0))
        self.assertEqual(plan.action, "run-now")
        self.assertTrue(plan.late_for_release)

    def test_absurdly_early_routine_is_refused(self):
        plan = ptbot.plan_wait(at(8, 0), at(12, 0), max_wait_minutes=120)
        self.assertEqual(plan.action, "too-early")

    def test_no_release_time_runs_now(self):
        plan = ptbot.plan_wait(at(11, 15), None)
        self.assertEqual(plan.action, "run-now")
        self.assertIsNone(plan.fire_at)


class LatenessTests(unittest.TestCase):
    def test_minutes_late(self):
        self.assertEqual(ptbot.lateness_minutes("11:15", at(11, 32, 30)), 17.5)

    def test_early(self):
        self.assertEqual(ptbot.lateness_minutes("11:15", at(11, 14)), -1.0)

    def test_no_slot(self):
        self.assertIsNone(ptbot.lateness_minutes(None, at(11, 14)))
        self.assertIsNone(ptbot.lateness_minutes("soon", at(11, 14)))

    def test_across_midnight(self):
        start = datetime(2026, 11, 14, 0, 10, tzinfo=LA)
        self.assertEqual(ptbot.lateness_minutes("23:55", start), 15.0)


class VerdictTests(unittest.TestCase):
    def test_booked_live(self):
        line = ptbot.verdict_line(
            {"mode": "live", "booked": True, "date_long": "Thu 19 Nov 2026", "time": "6:30 PM",
             "duration": 60, "confirmation": "explicit", "outcome": "booked"}
        )
        self.assertEqual(line, "BOOKED Thu 19 Nov 2026 at 6:30 PM for 60 min (explicit confirmation)")

    def test_rehearsal_is_always_labelled(self):
        line = ptbot.verdict_line(
            {"mode": "rehearsal", "dry_run": True, "booked": False, "date_long": "Thu 19 Nov 2026",
             "detail": "Dry run: 6:30 PM / 60min was available", "outcome": "not-booked"}
        )
        self.assertTrue(line.startswith("REHEARSAL (dry run, nothing saved): "))
        self.assertIn("6:30 PM", line)

    def test_skip_is_quiet(self):
        line = ptbot.verdict_line({"outcome": "skipped", "detail": "Sat 21 Nov is cycle day 5"})
        self.assertTrue(line.startswith("quiet - "))

    def test_error_is_failed(self):
        line = ptbot.verdict_line({"mode": "live", "error": "LoginError: rejected", "date_long": "Thu"})
        self.assertIn("FAILED", line)

    def test_not_booked_live(self):
        line = ptbot.verdict_line({"mode": "live", "booked": False, "date_long": "Thu", "outcome": "not-booked"})
        self.assertTrue(line.startswith("NOT BOOKED Thu"))


class UnverifiedVerdictTests(unittest.TestCase):
    def test_unverified_is_never_reported_as_not_booked(self):
        line = ptbot.verdict_line({"mode": "live", "booked": False, "confirmation": "unverified",
                                   "date_long": "Saturday, November 21, 2026", "time": "6:30 PM", "duration": 60,
                                   "detail": "the form closed after Confirm with no message either way"})
        self.assertTrue(line.startswith("UNVERIFIED Saturday, November 21, 2026 at 6:30 PM"))
        self.assertIn("Check your reservations in CourtReserve now", line)
        self.assertNotIn("NOT BOOKED", line)


class TimingSummaryTests(unittest.TestCase):
    def test_recommends_at_least_45_minutes(self):
        records = [{"late_minutes": m, "started_at": "x", "finished_at": "y"} for m in (2, 5, 9)]
        self.assertEqual(ptbot.summarize_timing(records)["recommended_offset_minutes"], 45)

    def test_recommends_more_after_very_late_starts(self):
        records = [{"late_minutes": m, "started_at": "x", "finished_at": "y"} for m in (12, 37)]
        summary = ptbot.summarize_timing(records)
        self.assertEqual(summary["recommended_offset_minutes"], 50)
        self.assertEqual(summary["late_max"], 37)

    def test_counts_cut_off_runs(self):
        records = [{"late_minutes": 3, "started_at": "x"}, {"late_minutes": 4, "started_at": "x", "finished_at": "y"}]
        self.assertEqual(ptbot.summarize_timing(records)["unfinished"], 1)


class ModeTests(unittest.TestCase):
    def test_go_live_refuses_without_owner_tap(self):
        args = ptbot.build_parser().parse_args(["go-live"])
        self.assertEqual(args.func(args), ptbot.EXIT_CONFIG)


if __name__ == "__main__":
    unittest.main()
