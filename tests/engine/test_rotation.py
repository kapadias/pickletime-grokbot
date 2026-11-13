"""The 2-2-3 rotation: which court dates are worth a booking run.

The offsets shipped in config/config.template.yaml encode a custody pattern that is easy to get
wrong by a day and hard to notice afterwards, so the shipped values are asserted
here against the weekdays they are meant to mean.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from pickletime.rotation import Rotation

# The values in config/config.template.yaml. Day 0 is the Monday of a "week A"; the free days
# that are also playable are week A Thursday, week B Monday, Friday and Saturday.
SHIPPED = Rotation(anchor_date="08/24/2026", cycle_days=14, play_on_offsets=[3, 7, 11, 12])


# -- the shipped rotation ---------------------------------------------------
def test_the_shipped_offsets_land_on_thursday_monday_friday_saturday() -> None:
    weekdays = [
        (SHIPPED.anchor_date.toordinal() + offset) for offset in SHIPPED.play_on_offsets
    ]
    assert [date.fromordinal(d).strftime("%A") for d in weekdays] == [
        "Thursday",
        "Monday",
        "Friday",
        "Saturday",
    ]


def test_the_free_wednesday_the_rotation_was_derived_from_is_not_itself_bookable() -> None:
    """08/26/2026 is free, but Wednesday is a committed evening."""
    free_wednesday = date(2026, 8, 26)
    assert SHIPPED.offset_of(free_wednesday) == 2
    assert SHIPPED.is_play_date(free_wednesday) is False


def test_the_shipped_rotation_yields_four_dates_per_fortnight() -> None:
    dates = SHIPPED.next_play_dates(after=date(2026, 8, 24), count=8)
    assert dates == [
        date(2026, 8, 27),  # Thu, week A
        date(2026, 8, 31),  # Mon, week B
        date(2026, 9, 4),   # Fri, week B
        date(2026, 9, 5),   # Sat, week B — mornings, see weekday_overrides
        date(2026, 9, 10),  # Thu, next cycle
        date(2026, 9, 14),
        date(2026, 9, 18),
        date(2026, 9, 19),
    ]
    assert {d.strftime("%A") for d in dates} == {
        "Monday", "Thursday", "Friday", "Saturday"
    }


def test_every_play_date_is_one_of_the_allowed_weekdays() -> None:
    """Sweep a year rather than trusting the first fortnight."""
    allowed = {"Monday", "Thursday", "Friday", "Saturday"}
    for day in SHIPPED.next_play_dates(after=date(2026, 8, 24), count=78):
        assert day.strftime("%A") in allowed, day


# -- offsets ----------------------------------------------------------------
def test_the_anchor_itself_is_cycle_day_zero() -> None:
    assert SHIPPED.offset_of(date(2026, 8, 24)) == 0


def test_offsets_repeat_every_cycle() -> None:
    assert SHIPPED.offset_of(date(2026, 9, 7)) == 0  # 14 days on
    assert SHIPPED.offset_of(date(2026, 9, 10)) == SHIPPED.offset_of(date(2026, 8, 27))


def test_dates_before_the_anchor_still_get_a_positive_offset() -> None:
    """Python's % is non-negative, so history works without special casing."""
    assert SHIPPED.offset_of(date(2026, 8, 23)) == 13
    assert SHIPPED.is_play_date(date(2026, 8, 13)) is True  # a full cycle before 08/27
    assert SHIPPED.is_play_date(date(2026, 8, 23)) is False  # Sunday: free, but not played


# -- parsing and validation -------------------------------------------------
def test_anchor_date_accepts_the_same_mm_dd_yyyy_the_rest_of_the_config_uses() -> None:
    assert Rotation(anchor_date="08/24/2026", play_on_offsets=[0]).anchor_date == date(
        2026, 8, 24
    )


def test_a_malformed_anchor_date_is_rejected_with_the_expected_format() -> None:
    with pytest.raises(ValidationError, match="MM/DD/YYYY"):
        Rotation(anchor_date="2026-08-24-ish", play_on_offsets=[0])


def test_an_offset_outside_the_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"within 0\.\.13"):
        Rotation(anchor_date="08/24/2026", cycle_days=14, play_on_offsets=[14])


def test_a_negative_offset_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"within 0\.\.13"):
        Rotation(anchor_date="08/24/2026", cycle_days=14, play_on_offsets=[-1])


def test_duplicate_offsets_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        Rotation(anchor_date="08/24/2026", play_on_offsets=[3, 3])


def test_a_rotation_with_no_offsets_is_rejected() -> None:
    """Otherwise the bot would silently never book anything again."""
    with pytest.raises(ValidationError):
        Rotation(anchor_date="08/24/2026", play_on_offsets=[])


def test_unknown_keys_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValidationError):
        Rotation(anchor_date="08/24/2026", play_on_offsets=[3], play_on_days=["Mon"])


# -- calendar ---------------------------------------------------------------
def test_calendar_names_the_day_it_books_and_the_day_it_fires() -> None:
    lines = SHIPPED.calendar(after=date(2026, 8, 25), count=1, days_in_advance=8)
    assert lines == ["play Thu 27 Aug 2026  (cycle day  3)   ← fires Wed 19 Aug"]


def test_calendar_omits_the_firing_day_when_nothing_is_booked_in_advance() -> None:
    (line,) = SHIPPED.calendar(after=date(2026, 8, 25), count=1)
    assert "fires" not in line


def test_next_play_dates_includes_the_start_day_itself() -> None:
    assert SHIPPED.next_play_dates(after=date(2026, 8, 27), count=1) == [date(2026, 8, 27)]


def test_next_play_dates_terminates_on_a_sparse_rotation() -> None:
    """One offset in a year-long cycle is the worst case for the scan bound."""
    sparse = Rotation(anchor_date="01/01/2026", cycle_days=366, play_on_offsets=[365])
    assert len(sparse.next_play_dates(after=date(2026, 1, 2), count=3)) == 3


# -- per-weekday time preferences -------------------------------------------
SATURDAY_MORNINGS = {
    "Saturday": {
        "search_start_time": "9:00 AM",
        "search_end_time": "10:30 AM",
        "priority_time_slots": ["9:00 AM", "10:00 AM", "9:30 AM", "10:30 AM"],
    }
}


def _settings(**kw):
    from pickletime.settings import BookingSettings

    base = {
        "search_start_time": "5:30 PM",
        "search_end_time": "7:30 PM",
        "priority_time_slots": ["6:30 PM", "7:00 PM"],
        "weekday_overrides": SATURDAY_MORNINGS,
    }
    return BookingSettings(**{**base, **kw})


def test_saturday_gets_its_own_window() -> None:
    saturday = _settings().for_date(date(2026, 9, 5))
    assert saturday.search_start_time.strftime("%I:%M %p") == "09:00 AM"
    assert saturday.search_end_time.strftime("%I:%M %p") == "10:30 AM"
    assert saturday.priority_time_slots == ["9:00 AM", "10:00 AM", "9:30 AM", "10:30 AM"]


def test_every_other_day_keeps_the_evening_window() -> None:
    for day in (date(2026, 9, 4), date(2026, 9, 10), date(2026, 9, 14)):
        resolved = _settings().for_date(day)
        assert resolved.search_start_time.strftime("%I:%M %p") == "05:30 PM", day
        assert resolved.priority_time_slots == ["6:30 PM", "7:00 PM"], day


def test_an_override_leaves_unrelated_settings_alone() -> None:
    resolved = _settings(preferred_durations=[60, 30]).for_date(date(2026, 9, 5))
    assert resolved.preferred_durations == [60, 30]
    assert resolved.organization_id == 13233
    assert resolved.rotation is None


def test_settings_without_overrides_return_themselves_unchanged() -> None:
    from pickletime.settings import BookingSettings

    base = BookingSettings()
    assert base.for_date(date(2026, 9, 5)) is base


def test_weekday_names_are_case_insensitive() -> None:
    from pickletime.settings import BookingSettings

    settings = BookingSettings(weekday_overrides={"saturday": {"search_start_time": "9:00 AM"}})
    assert "Saturday" in settings.weekday_overrides
    assert settings.for_date(date(2026, 9, 5)).search_start_time.hour == 9


def test_a_misspelled_weekday_is_rejected_rather_than_ignored() -> None:
    """It would otherwise never match, and book the default window forever."""
    from pickletime.settings import BookingSettings

    with pytest.raises(ValidationError, match="not a weekday name"):
        BookingSettings(weekday_overrides={"Saturdays": {"search_start_time": "9:00 AM"}})


def test_an_override_that_inverts_the_window_is_rejected() -> None:
    """Re-validated on resolve, not merged blindly into an empty search."""
    from pickletime.settings import BookingSettings

    settings = BookingSettings(
        weekday_overrides={"Saturday": {"search_start_time": "11:00 AM",
                                        "search_end_time": "9:00 AM"}}
    )
    with pytest.raises(ValidationError, match="search_end_time"):
        settings.for_date(date(2026, 9, 5))


def test_unknown_keys_in_an_override_are_rejected() -> None:
    from pickletime.settings import BookingSettings

    with pytest.raises(ValidationError):
        BookingSettings(weekday_overrides={"Saturday": {"start_time": "9:00 AM"}})


def test_the_shipped_config_books_saturday_mornings_and_weekday_evenings() -> None:
    """The whole point of the override, asserted against the shipped config template."""
    from pathlib import Path

    import yaml

    from pickletime.settings import BookingSettings

    root = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((root / "config" / "config.template.yaml").read_text(encoding="utf-8"))
    settings = BookingSettings(**raw)
    rotation = settings.rotation
    assert rotation is not None

    for day in rotation.next_play_dates(after=date(2026, 8, 24), count=8):
        resolved = settings.for_date(day)
        if day.strftime("%A") == "Saturday":
            assert resolved.search_start_time.hour == 9, day
            assert resolved.search_end_time.strftime("%I:%M %p") == "10:30 AM", day
        else:
            assert resolved.search_start_time.strftime("%I:%M %p") == "05:30 PM", day
