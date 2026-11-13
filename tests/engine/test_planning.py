"""Slot planning and URL construction — no browser needed."""

from __future__ import annotations

from datetime import date

import pytest

from pickletime.services.booking import BookingService, SlotCandidate
from pickletime.settings import BookingSettings

TARGET = date(2025, 7, 22)


def service(**overrides: object) -> BookingService:
    defaults: dict[str, object] = {
        "search_start_time": "10:00 AM",
        "search_end_time": "12:00 PM",
        "time_increment": 30,
        "preferred_durations": [60, 30],
        "booking_priority": "duration",
    }
    defaults.update(overrides)
    return BookingService(None, None, BookingSettings(**defaults))  # type: ignore[arg-type]


def labels(plan: list[SlotCandidate]) -> list[tuple[str, int]]:
    return [(candidate.label, candidate.duration) for candidate in plan]


def test_window_is_inclusive_of_both_ends() -> None:
    plan = service(preferred_durations=[60]).build_plan(TARGET)
    assert [c.label for c in plan] == [
        "10:00 AM", "10:30 AM", "11:00 AM", "11:30 AM", "12:00 PM"
    ]


def test_duration_priority_sweeps_every_slot_at_the_best_duration_first() -> None:
    plan = service(search_end_time="11:00 AM").build_plan(TARGET)
    assert labels(plan) == [
        ("10:00 AM", 60), ("10:30 AM", 60), ("11:00 AM", 60),
        ("10:00 AM", 30), ("10:30 AM", 30), ("11:00 AM", 30),
    ]


def test_time_priority_exhausts_durations_at_each_slot() -> None:
    plan = service(search_end_time="11:00 AM", booking_priority="time").build_plan(TARGET)
    assert labels(plan) == [
        ("10:00 AM", 60), ("10:00 AM", 30),
        ("10:30 AM", 60), ("10:30 AM", 30),
        ("11:00 AM", 60), ("11:00 AM", 30),
    ]


def test_priority_slots_are_tried_first_in_the_configured_order() -> None:
    plan = service(
        preferred_durations=[60], priority_time_slots=["11:30 AM", "10:30 AM"]
    ).build_plan(TARGET)
    assert [c.label for c in plan] == [
        "11:30 AM", "10:30 AM", "10:00 AM", "11:00 AM", "12:00 PM"
    ]


def test_priority_slots_outside_the_window_are_ignored_not_invented() -> None:
    plan = service(preferred_durations=[60], priority_time_slots=["8:00 PM"]).build_plan(TARGET)
    assert "8:00 PM" not in [c.label for c in plan]
    assert len(plan) == 5


def test_custom_increment_changes_the_candidate_stride() -> None:
    plan = service(preferred_durations=[60], time_increment=60).build_plan(TARGET)
    assert [c.label for c in plan] == ["10:00 AM", "11:00 AM", "12:00 PM"]


def test_single_moment_window_yields_one_slot() -> None:
    plan = service(
        preferred_durations=[60], search_start_time="10:00 AM", search_end_time="10:00 AM"
    ).build_plan(TARGET)
    assert [c.label for c in plan] == ["10:00 AM"]


# -- the slot URL -----------------------------------------------------------
def test_data_href_matches_the_live_sites_format() -> None:
    """Pinned against a real ``data-href`` captured from CourtReserve.

    The previous implementation emitted ``courtType`` before ``customSchedulerId``
    and omitted ``courtTypeId`` entirely, so the exact-match fast path never hit a
    single slot on the live grid.
    """
    booking = service()
    candidate = booking.build_plan(TARGET)[0]
    assert booking.data_href(candidate) == (
        "/Online/Reservations/CreateReservation/13233"
        "?start=7/22/2025%2010:00%20AM"
        "&end=7/22/2025%2010:30%20AM"
        "&customSchedulerId=16984"
        "&courtType=Pickleball"
    )


def test_data_href_includes_court_type_id_when_configured() -> None:
    booking = service(court_type_id=9)
    href = booking.data_href(booking.build_plan(TARGET)[0])
    assert href.endswith("&customSchedulerId=16984&courtTypeId=9&courtType=Pickleball")


def test_href_matching_survives_parameter_reordering() -> None:
    """Matching keys on the start time and scheduler id, not the whole string.

    This is what actually found the one bookable slot in the first live run,
    while the exact comparison was failing on every single one.
    """
    booking = service()
    candidate = booking.build_plan(TARGET)[0]
    live = (
        "/Online/Reservations/CreateReservation/13233"
        "?start=7/22/2025%2010:00%20AM&end=7/22/2025%2010:30%20AM"
        "&customSchedulerId=16984&courtTypeId=9&courtType=Pickleball"
    )
    needles = booking._start_needles(candidate)
    assert any(n in live for n in needles)
    assert "customSchedulerId=16984" in live


def test_data_href_has_no_zero_padding() -> None:
    booking = service(search_start_time="9:00 AM", search_end_time="9:00 AM")
    candidate = booking.build_plan(date(2025, 7, 2))[0]
    href = booking.data_href(candidate)
    assert "start=7/2/2025%209:00%20AM" in href
    assert "/07/" not in href and "%2009:" not in href


def test_data_href_respects_the_grid_cell_length() -> None:
    booking = service(slot_length_minutes=60, search_end_time="10:00 AM")
    candidate = booking.build_plan(TARGET)[0]
    assert "end=7/22/2025%2011:00%20AM" in booking.data_href(candidate)


def test_data_href_end_rolls_over_midnight_correctly() -> None:
    booking = service(search_start_time="11:45 PM", search_end_time="11:45 PM")
    candidate = booking.build_plan(TARGET)[0]
    href = booking.data_href(candidate)
    assert "start=7/22/2025%2011:45%20PM" in href
    assert "end=7/23/2025%2012:15%20AM" in href


def test_start_needles_cover_encoded_and_raw_forms() -> None:
    booking = service()
    candidate = booking.build_plan(TARGET)[0]
    assert booking._start_needles(candidate) == [
        "start=7/22/2025%2010:00%20AM",
        "start=7/22/2025 10:00 AM",
    ]


@pytest.mark.parametrize("org,sport", [(1, 2), (13233, 16984), (99999, 12345)])
def test_ids_flow_through_to_the_url(org: int, sport: int) -> None:
    booking = service(organization_id=org, sport_id=sport)
    href = booking.data_href(booking.build_plan(TARGET)[0])
    assert f"/CreateReservation/{org}?" in href
    assert f"customSchedulerId={sport}" in href
