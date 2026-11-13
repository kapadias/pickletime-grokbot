"""Clock and calendar helpers — the source of the wrong-day booking bug."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from pickletime.timeutil import (
    format_clock_time,
    format_long_date,
    format_us_date,
    next_occurrence_of,
    parse_clock_time,
    parse_us_date,
    resolve_court_date,
    sleep_until,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7:30 PM", time(19, 30)),
        ("07:30 PM", time(19, 30)),
        ("7:30 pm", time(19, 30)),
        ("12:00 PM", time(12, 0)),
        ("12:00 AM", time(0, 0)),
        ("10:00 AM", time(10, 0)),
        ("19:30", time(19, 30)),
    ],
)
def test_parse_clock_time(raw: str, expected: time) -> None:
    assert parse_clock_time(raw) == expected


def test_parse_clock_time_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="7:30 PM"):
        parse_clock_time("half past seven")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (time(19, 30), "7:30 PM"),
        (time(9, 0), "9:00 AM"),
        (time(12, 0), "12:00 PM"),
        (time(0, 30), "12:30 AM"),
        (time(11, 30), "11:30 AM"),
    ],
)
def test_format_clock_time_matches_the_sites_style(value: time, expected: str) -> None:
    """No leading zero on the hour — the site's data-href is matched verbatim."""
    assert format_clock_time(value) == expected


def test_date_formatters_avoid_platform_specific_strftime() -> None:
    """The old code used %-d/%-m, which is glibc-only and raises on Windows."""
    assert format_us_date(date(2025, 7, 2)) == "7/2/2025"
    assert format_us_date(date(2025, 12, 25)) == "12/25/2025"
    assert format_long_date(date(2025, 7, 2)) == "Wednesday, July 2, 2025"


def test_parse_us_date_roundtrip() -> None:
    assert parse_us_date("07/22/2025") == date(2025, 7, 22)


def test_court_date_uses_the_facility_timezone_not_the_servers() -> None:
    """22:00 UTC is still the previous day in Los Angeles.

    Computing the target from a naive ``datetime.now()`` on a UTC container —
    what the old code did — books a day too far ahead for every run after
    17:00 Pacific.
    """
    utc_evening = datetime(2025, 7, 10, 22, 30, tzinfo=UTC)  # 15:30 on the 10th in LA

    assert resolve_court_date(8, PACIFIC, now=utc_evening) == date(2025, 7, 18)

    utc_after_midnight = datetime(2025, 7, 11, 3, 0, tzinfo=UTC)  # 20:00 on the 10th in LA
    assert resolve_court_date(8, PACIFIC, now=utc_after_midnight) == date(2025, 7, 18)
    assert resolve_court_date(8, UTC, now=utc_after_midnight) == date(2025, 7, 19)


def test_next_occurrence_is_today_when_still_ahead() -> None:
    now = datetime(2025, 7, 10, 11, 55, tzinfo=PACIFIC)
    assert next_occurrence_of(time(12, 0), PACIFIC, now=now) == datetime(
        2025, 7, 10, 12, 0, tzinfo=PACIFIC
    )


def test_next_occurrence_rolls_to_tomorrow_once_past() -> None:
    now = datetime(2025, 7, 10, 12, 30, tzinfo=PACIFIC)
    assert next_occurrence_of(time(12, 0), PACIFIC, now=now) == datetime(
        2025, 7, 11, 12, 0, tzinfo=PACIFIC
    )


def test_next_occurrence_tolerates_a_late_trigger() -> None:
    """A scheduler firing 30s late must sprint now, not arm for tomorrow."""
    now = datetime(2025, 7, 10, 12, 0, 30, tzinfo=PACIFIC)
    assert next_occurrence_of(
        time(12, 0), PACIFIC, now=now, tolerance_seconds=120
    ) == datetime(2025, 7, 10, 12, 0, tzinfo=PACIFIC)


def test_sleep_until_waits_then_returns() -> None:
    clock = {"now": datetime(2025, 7, 10, 11, 59, 50, tzinfo=PACIFIC)}
    slept: list[float] = []

    def now_fn() -> datetime:
        return clock["now"]

    def sleep_fn(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    target = datetime(2025, 7, 10, 12, 0, 0, tzinfo=PACIFIC)
    waited = sleep_until(target, now_fn=now_fn, sleep_fn=sleep_fn)

    assert waited == pytest.approx(10.0, abs=0.05)
    assert clock["now"] >= target
    # Coarse chunks first, then fine steps as it closes in.
    assert max(slept) <= 5.0
    assert min(slept) <= 0.01


def test_sleep_until_respects_the_guard_rail() -> None:
    """A misconfigured release time must not park the process for hours."""
    clock = {"now": datetime(2025, 7, 10, 12, 0, tzinfo=PACIFIC)}

    def now_fn() -> datetime:
        return clock["now"]

    def sleep_fn(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    waited = sleep_until(
        datetime(2025, 7, 11, 12, 0, tzinfo=PACIFIC),
        now_fn=now_fn,
        sleep_fn=sleep_fn,
        max_wait_seconds=30,
    )
    assert waited == pytest.approx(30.0, abs=1.0)


def test_sleep_until_requires_an_aware_target() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sleep_until(datetime(2025, 7, 10, 12, 0))
