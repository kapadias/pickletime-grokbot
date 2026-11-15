"""Schedules from interview answers, and the runner's propose / apply flow."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from pickletime.schedule import (
    ScheduleError,
    build_config,
    config_diff,
    describe,
    dump_config,
    parse_facility_url,
    routine_slot,
    weekly_rotation,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = yaml.safe_load((ROOT / "config" / "config.template.yaml").read_text(encoding="utf-8"))
URL = "https://app.courtreserve.com/Online/Reservations/Bookings/12345?sId=678"
TODAY = date(2026, 11, 13)  # a Friday

#: Shaped like an established owner's config, with keys the interview never asks about.
ESTABLISHED = {
    "organization_id": 11111, "sport_id": 222, "sport_name": "Pickleball", "court_type": "Pickleball",
    "court_type_id": 9, "duration_labels": {60: "60 Minutes"}, "timezone": "America/Los_Angeles",
    "release_time": "12:00 PM", "days_in_advance": 8, "search_start_time": "5:30 PM",
    "search_end_time": "7:30 PM", "priority_time_slots": ["6:30 PM", "7:00 PM"],
    "preferred_durations": [60, 30], "sprint_seconds": 25, "release_grace_seconds": 180,
    "rotation": {"anchor_date": "08/24/2026", "cycle_days": 14, "play_on_offsets": [3, 7, 11, 12]},
    "weekday_overrides": {"Saturday": {"search_start_time": "9:00 AM", "search_end_time": "10:30 AM"}},
}


# -- pieces -------------------------------------------------------------------
def test_facility_from_a_booking_link():
    assert parse_facility_url(URL) == {"organization_id": 12345, "sport_id": 678,
                                       "base_url": "https://app.courtreserve.com"}


def test_facility_from_a_portal_link_has_no_sport():
    assert parse_facility_url("https://app.courtreserve.com/Online/Portal/Index/12345") == {
        "organization_id": 12345, "base_url": "https://app.courtreserve.com"}


@pytest.mark.parametrize("bad", ["my club", "https://example.com/", "app.courtreserve.com/Bookings"])
def test_a_link_without_a_club_says_what_to_paste(bad):
    with pytest.raises(ScheduleError, match="booking page|web link"):
        parse_facility_url(bad)


def test_weekdays_become_a_monday_anchored_week():
    rotation = weekly_rotation(["Thursday", "mon", "Monday"])
    assert rotation == {"anchor_date": "01/05/2026", "cycle_days": 7, "play_on_offsets": [0, 3]}


def test_routine_runs_45_minutes_before_release():
    assert routine_slot("12:00 PM") == "11:15"
    assert routine_slot("7:00 AM") == "06:15"
    assert routine_slot(None) is None


# -- a first schedule ------------------------------------------------------------
def test_a_new_owner_gets_a_complete_valid_schedule():
    config, warnings = build_config(TEMPLATE, {
        "facility_url": URL, "timezone": "America/New_York", "release_time": "7:00 AM",
        "days_in_advance": 7, "play_days": ["Tuesday", "Thursday"],
        "window": {"start": "6:00 PM", "end": "8:00 PM"}, "priority_times": ["7:00 PM", "6:30 PM"],
        "durations": [90, 60],
        "weekday_windows": {"Thursday": {"start": "7:00 AM", "end": "9:00 AM", "priority_times": ["7:30 AM"]}},
    })
    assert warnings == []
    assert (config["organization_id"], config["sport_id"]) == (12345, 678)
    assert config["rotation"]["play_on_offsets"] == [1, 3]
    text = "\n".join(describe(config, TODAY))
    assert "Play days: every Tuesday, Thursday" in text
    assert "7:00 AM America/New_York, 7 days ahead -> the book routine runs daily at 06:15" in text
    assert "Thursday: 7:00 AM-9:00 AM, tries 7:30 AM" in text
    play_lines = [ln for ln in text.splitlines() if ln.startswith("  ") and "booked on" in ln]
    assert len(play_lines) == 6 and all(ln.strip()[:3] in {"Tue", "Thu"} for ln in play_lines)


def test_no_club_is_refused_with_the_next_step():
    with pytest.raises(ScheduleError, match="no club yet"):
        build_config(TEMPLATE, {"play_days": ["Monday"]})


def test_a_link_without_a_sport_is_refused():
    with pytest.raises(ScheduleError, match="no sport"):
        build_config(TEMPLATE, {"facility_url": "https://app.courtreserve.com/Online/Portal/Index/12345"})


@pytest.mark.parametrize("answers,match", [
    ({"facility_url": URL, "window": {"start": "6 pm-ish", "end": "8:00 PM"}}, "not a time"),
    ({"facility_url": URL, "play_days": ["Funday"]}, "not a weekday"),
    ({"facility_url": URL, "favourite_court": 3}, "unknown answer"),
    ({"facility_url": URL, "window": {"start": "8:00 PM", "end": "6:00 PM"}}, "not valid"),
    ({"facility_url": URL, "play_days": ["Monday"], "rotation": {"cycle_days": 7}}, "not both"),
])
def test_impossible_answers_are_explained(answers, match):
    with pytest.raises(ScheduleError, match=match):
        build_config(TEMPLATE, answers)


def test_a_priority_outside_the_window_is_flagged():
    _, warnings = build_config(TEMPLATE, {"facility_url": URL, "priority_times": ["9:00 AM"]})
    assert any("9:00 AM is outside the window" in w for w in warnings)


def test_rolling_release_drops_the_release_time():
    config, warnings = build_config(TEMPLATE, {"facility_url": URL, "release_time": None})
    assert "release_time" not in config
    assert any("no release time" in w for w in warnings)
    assert "rolling basis" in "\n".join(describe(config, TODAY))


# -- changing an existing schedule ----------------------------------------------------
def test_no_answers_keeps_the_schedule_exactly():
    config, warnings = build_config(ESTABLISHED, {})
    assert config == ESTABLISHED and warnings == []


def test_one_change_touches_only_that_key():
    config, _ = build_config(ESTABLISHED, {"priority_times": ["7:00 PM", "6:30 PM"]})
    assert config["priority_time_slots"] == ["7:00 PM", "6:30 PM"]
    assert {k: v for k, v in config.items() if k != "priority_time_slots"} == {
        k: v for k, v in ESTABLISHED.items() if k != "priority_time_slots"}


def test_moving_club_drops_club_specific_keys():
    config, warnings = build_config(ESTABLISHED, {"facility_url": URL})
    assert "court_type_id" not in config and "duration_labels" not in config
    assert any("court_type_id" in w for w in warnings)


def test_every_day_removes_the_rotation():
    config, _ = build_config(ESTABLISHED, {"play_days": "every day"})
    assert "rotation" not in config
    assert "Play days: every day" in describe(config, TODAY)


def test_a_long_rotation_is_described_as_a_cycle():
    text = "\n".join(describe(ESTABLISHED, TODAY))
    assert "days 3, 7, 11, 12 of a 14-day cycle starting Mon 24 Aug 2026" in text
    assert "Saturday: 9:00 AM-10:30 AM" in text


def test_the_diff_names_the_changed_lines():
    new, _ = build_config(ESTABLISHED, {"durations": [90]})
    changes = config_diff(dump_config(ESTABLISHED), dump_config(new))
    assert any(c.startswith("-") and "60" in c for c in changes)
    assert any(c.startswith("+") and "90" in c for c in changes)


# -- the runner ---------------------------------------------------------------
@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ptbot_schedule", ROOT / "scripts" / "ptbot.py")
    ptbot = importlib.util.module_from_spec(spec)
    sys.modules["ptbot_schedule"] = ptbot
    spec.loader.exec_module(ptbot)
    for name, value in {
        "DESK": tmp_path, "STATE": tmp_path / "state", "CONFIG": tmp_path / "config.yaml",
        "PROPOSALS": tmp_path / "proposals", "BACKUPS": tmp_path / "backups",
        "PROPOSED": tmp_path / "proposals" / "config.proposed.yaml", "MODE_FILE": tmp_path / "state" / "mode",
    }.items():
        monkeypatch.setattr(ptbot, name, value)
    (tmp_path / "config.yaml").write_text((ROOT / "config" / "config.template.yaml").read_text(encoding="utf-8"),
                                          encoding="utf-8")
    return ptbot


def run(ptbot, *argv):
    return ptbot.main(list(argv))


def test_a_fresh_bot_refuses_to_book_anywhere(runner):
    from pickletime.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="names no club yet"):
        runner._load_settings(dry_run=True, require_credentials=False)


def test_propose_then_apply_with_the_owners_tap(runner, capsys):
    before = runner.CONFIG.read_text(encoding="utf-8")
    answers = json.dumps({"facility_url": URL, "play_days": ["Monday"], "release_time": "8:00 AM"})
    assert run(runner, "schedule", "propose", "--answers", answers) == 0
    out = capsys.readouterr().out
    assert "PROPOSED schedule (not in use yet)" in out and "every Monday" in out
    assert runner.CONFIG.read_text(encoding="utf-8") == before  # nothing live changed

    assert run(runner, "schedule", "apply") == runner.EXIT_CONFIG
    assert "refused" in capsys.readouterr().out

    assert run(runner, "schedule", "apply", "--owner-tapped") == 0
    out = capsys.readouterr().out
    assert "APPLIED" in out
    assert "ACTION: move the daily book routine to 07:15" in out
    assert yaml.safe_load(runner.CONFIG.read_text(encoding="utf-8"))["organization_id"] == 12345
    assert list(runner.BACKUPS.glob("config-*.yaml"))
    assert not runner.PROPOSED.exists()
    settings = runner._load_settings(dry_run=True, require_credentials=False)
    assert settings.booking.organization_id == 12345


def test_keeping_the_schedule_changes_nothing(runner, capsys):
    runner.CONFIG.write_text(dump_config(ESTABLISHED), encoding="utf-8")
    before = runner.CONFIG.read_text(encoding="utf-8")
    assert run(runner, "schedule", "show") == 0
    assert "14-day cycle" in capsys.readouterr().out
    assert run(runner, "schedule", "propose", "--answers", "{}") == 0
    assert "no change from the current schedule" in capsys.readouterr().out
    assert run(runner, "schedule", "discard") == 0
    assert runner.CONFIG.read_text(encoding="utf-8") == before


def test_bad_answers_write_no_proposal(runner, capsys):
    assert run(runner, "schedule", "propose", "--answers", '{"play_days": ["Monday"]}') == runner.EXIT_CONFIG
    assert "no club yet" in capsys.readouterr().out
    assert not runner.PROPOSED.exists()
