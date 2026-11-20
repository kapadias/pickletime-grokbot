"""Booking on the 19 Nov 2026 scheduler: Reserve buttons, no slot testids.

The fake (tests/engine/fake_portal_grid.py) follows the scrubbed capture of that
page. The older portal fixture still covers the 14 Nov testids.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fake_portal import PortalState
from fake_portal_grid import FakePortalGrid

from pickletime.bot import CourtBookingBot
from pickletime.core.browser import BrowserManager
from pickletime.ledger import NullLedger
from pickletime.services.auth import AuthService
from pickletime.services.navigation import NavigationService
from pickletime.services.portal import PortalBookingService


def _settings(make_settings, chrome_paths, **overrides):
    binary, driver = chrome_paths
    base = {
        "chrome_binary": binary,
        "days_in_advance": 8,
        "search_start_time": "5:30 PM",
        "search_end_time": "7:30 PM",
        "priority_time_slots": ["6:30 PM", "7:00 PM"],
        "preferred_durations": [60, 30],
        "sprint_seconds": 8,
        "release_time": None,
    }
    if driver:
        base["chrome_driver_path"] = driver
    base.update(overrides)
    return make_settings(**base)


def _run(make_settings, chrome_paths, state_kwargs=None, **overrides):
    probe = _settings(make_settings, chrome_paths, **overrides)
    target = probe.booking.target_date()
    state = PortalState(today=target - timedelta(days=8), form_delay_ms=600, **(state_kwargs or {}))
    if not state.open_slots:
        state.open_slots = {target: {"6:30 PM": 2, "7:00 PM": 1}}
    with FakePortalGrid(state) as site:
        settings = _settings(make_settings, chrome_paths, base_url=site.base_url, **overrides)
        report = CourtBookingBot(settings, ledger=NullLedger()).run()
    return report, state, target


@pytest.mark.e2e
def test_books_the_priority_slot_on_the_reserve_button_grid(make_settings, chrome_paths):
    report, state, target = _run(make_settings, chrome_paths)
    assert report.error is None, report.error
    assert report.booked is True
    assert (report.slot_time, report.duration, report.confirmation) == ("6:30 PM", 60, "explicit")
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]
    assert state.reads[-1] == target.isoformat()


@pytest.mark.e2e
def test_a_refusal_dialog_is_closed_and_the_next_slot_books(make_settings, chrome_paths):
    """CreateReservation said no: the modal has Close and no duration control."""
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(
        make_settings, chrome_paths,
        state_kwargs={
            "open_slots": {target: {"6:30 PM": 1, "7:00 PM": 1, "7:30 PM": 1}},
            "refuse_open": {"6:30 PM"},
        },
    )
    assert report.booked is True, report.detail or report.error
    assert state.bookings == [{"date": target.isoformat(), "slot": "7:00 PM", "duration": "60"}]
    assert state.book_requests == 1
    assert "6:30 PM" in state.forms_opened


@pytest.mark.e2e
def test_a_dry_run_never_confirms_on_the_new_grid(make_settings, chrome_paths):
    report, state, _ = _run(make_settings, chrome_paths, dry_run=True)
    assert report.booked is False
    assert report.dry_run is True
    assert state.bookings == []
    assert state.forms_opened  # the form was opened and filled


@pytest.mark.e2e
def test_never_confirms_a_form_showing_a_different_time_on_the_new_grid(make_settings, chrome_paths):
    report, state, _ = _run(make_settings, chrome_paths, state_kwargs={"modal_time_offset_min": 30})
    assert report.booked is False
    assert state.bookings == []


@pytest.mark.e2e
def test_a_shorter_duration_is_chosen_when_the_long_one_is_not_offered(make_settings, chrome_paths):
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(
        make_settings, chrome_paths,
        state_kwargs={
            "open_slots": {target: {"6:30 PM": 1, "7:00 PM": 1}},
            "durations_by_slot": {"6:30 PM": ["30 minutes"]},
        },
    )
    assert report.booked is True, report.detail or report.error
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "30"}]


@pytest.mark.e2e
def test_a_length_that_cannot_fit_does_not_open_the_form(make_settings, chrome_paths):
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(
        make_settings, chrome_paths,
        state_kwargs={"open_slots": {target: {"6:30 PM": 1, "7:30 PM": 1}}},
    )
    assert report.booked is True, report.detail or report.error
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "30"}]
    assert state.forms_opened == ["6:30 PM"]


@pytest.mark.e2e
def test_refresh_reaches_a_day_that_opens_late(make_settings, chrome_paths):
    settings = _settings(make_settings, chrome_paths)
    target = settings.booking.target_date()
    state = PortalState(
        today=target - timedelta(days=8), released=False, form_delay_ms=200,
        open_slots={target: {"7:00 PM": 1, "7:30 PM": 1}},
    )
    with FakePortalGrid(state) as site:
        booking_settings = _settings(make_settings, chrome_paths, base_url=site.base_url).booking
        with BrowserManager(booking_settings) as (driver, waiter):
            AuthService(driver, waiter, booking_settings, settings.credentials).login()
            navigation = NavigationService(driver, waiter, booking_settings)
            navigation.open_booking_grid()
            assert navigation.layout == "portal"
            service = PortalBookingService(driver, waiter, booking_settings)
            assert service.try_select_date(target) is None
            threading.Timer(1.5, lambda: setattr(state, "released", True)).start()
            started = time.monotonic()
            service.await_date(target, timeout=30)
            assert time.monotonic() - started < 25
            result = service.sprint(service.build_plan(target), deadline=waiter.now() + 8)
    assert result.booked is True, result.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "7:00 PM", "duration": "60"}]
