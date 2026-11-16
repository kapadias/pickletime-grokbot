"""Booking on the member portal layout, end to end against a fake portal.

The fake (tests/engine/fake_portal.py) is built from scrubbed captures of the
live portal. Assertions are on what the *site* recorded (``state.bookings``),
not only on what the bot reported.

One thing the fake cannot vouch for: what the live site shows after Confirm.
The fake shows a "Reservation created successfully" toast; the live wording is
unknown until the first real booking, which the service logs in full.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest
from fake_portal import FakePortal, PortalState

from pickletime.bot import CourtBookingBot
from pickletime.core.browser import BrowserManager
from pickletime.ledger import NullLedger
from pickletime.services.auth import AuthService
from pickletime.services.booking import SlotCandidate
from pickletime.services.navigation import NavigationService
from pickletime.services.portal import PortalBookingService, parse_portal_label


# -- pure helpers -------------------------------------------------------------
def test_label_parsing_picks_the_nearest_year():
    assert parse_portal_label("Saturday, November 14", date(2026, 11, 13)) == date(2026, 11, 14)
    # Stepping from late December into January must not land a year early.
    assert parse_portal_label("Friday, January 1", date(2026, 12, 28)) == date(2027, 1, 1)
    assert parse_portal_label("Thursday, December 31", date(2027, 1, 2)) == date(2026, 12, 31)
    assert parse_portal_label("Pickleball Reservations", date(2026, 11, 13)) is None
    assert parse_portal_label(None, date(2026, 11, 13)) is None


def test_the_form_check_matches_only_the_intended_slot(make_settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    service = PortalBookingService(None, None, make_settings().booking)
    slot = SlotCandidate(datetime(2026, 11, 21, 18, 30, tzinfo=ZoneInfo("America/Los_Angeles")), 60)
    live = "NOV 21 Saturday 6:30 PM – 7:00 PM Open Play Pickleball"
    assert service.form_shows(live, slot)
    assert not service.form_shows("NOV 21 Saturday 7:00 PM – 7:30 PM", slot)   # wrong time
    assert not service.form_shows("NOV 14 Saturday 6:30 PM – 7:00 PM", slot)   # wrong day
    assert not service.form_shows("NOV 21 Saturday 16:30 PM – 7:00 PM", slot)  # not a prefix match
    assert not service.form_shows(None, slot)


# -- end to end -----------------------------------------------------------------

def _settings(make_settings, chrome_paths, **overrides):
    binary, driver = chrome_paths
    base = {
        "chrome_binary": binary,
        "days_in_advance": 8,
        "search_start_time": "5:30 PM",
        "search_end_time": "7:30 PM",
        "priority_time_slots": ["6:30 PM", "7:00 PM"],
        "preferred_durations": [60, 30],
        "sprint_seconds": 6,
        "release_time": None,
    }
    if driver:
        base["chrome_driver_path"] = driver
    base.update(overrides)
    return make_settings(**base)


def _run(make_settings, chrome_paths, state_kwargs=None, **overrides):
    probe = _settings(make_settings, chrome_paths, **overrides)
    target = probe.booking.target_date()
    state = PortalState(today=target - timedelta(days=8), **(state_kwargs or {}))
    if not state.open_slots:
        state.open_slots = {target: {"6:30 PM": 2, "7:00 PM": 1}}
    with FakePortal(state) as site:
        settings = _settings(make_settings, chrome_paths, base_url=site.base_url, **overrides)
        report = CourtBookingBot(settings, ledger=NullLedger()).run()
    return report, state, target


@pytest.mark.e2e
def test_books_the_priority_slot_at_the_preferred_duration(make_settings, chrome_paths):
    report, state, target = _run(make_settings, chrome_paths)
    assert report.error is None, report.error
    assert report.booked is True
    assert (report.slot_time, report.duration, report.confirmation) == ("6:30 PM", 60, "explicit")
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]


@pytest.mark.e2e
def test_falls_back_to_a_shorter_duration_the_slot_offers(make_settings, chrome_paths):
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(
        make_settings, chrome_paths,
        state_kwargs={"open_slots": {target: {"6:30 PM": 1}}, "durations_by_slot": {"6:30 PM": ["30 minutes"]}},
    )
    assert report.booked is True, report.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "30"}]


@pytest.mark.e2e
def test_a_dry_run_fills_the_form_but_never_confirms(make_settings, chrome_paths):
    report, state, _ = _run(make_settings, chrome_paths, dry_run=True)
    assert report.booked is False
    assert report.dry_run is True
    assert "would have been booked" in (report.detail or "")
    assert state.bookings == []


@pytest.mark.e2e
def test_never_confirms_a_form_showing_a_different_time(make_settings, chrome_paths):
    report, state, _ = _run(make_settings, chrome_paths, state_kwargs={"modal_time_offset_min": 30})
    assert report.booked is False
    assert state.bookings == []


@pytest.mark.e2e
def test_a_refused_slot_moves_on_to_the_next(make_settings, chrome_paths):
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(make_settings, chrome_paths, state_kwargs={
        "reject_slots": {"6:30 PM"}, "open_slots": {target: {"6:30 PM": 1, "7:00 PM": 1, "7:30 PM": 1}}})
    assert report.booked is True, report.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "7:00 PM", "duration": "60"}]


@pytest.mark.e2e
def test_steps_through_days_that_load_slowly(make_settings, chrome_paths):
    """The live toolbar is disabled while a day loads; clicking then does nothing."""
    report, state, target = _run(make_settings, chrome_paths, state_kwargs={"load_delay_ms": 1200})
    assert report.booked is True, report.detail or report.error
    assert state.reads[-1] == target.isoformat()


@pytest.mark.e2e
def test_takes_the_new_day_the_moment_it_is_released(make_settings, chrome_paths):
    settings = _settings(make_settings, chrome_paths)
    target = settings.booking.target_date()
    state = PortalState(today=target - timedelta(days=8), released=False,
                        open_slots={target: {"7:00 PM": 1, "7:30 PM": 1}})
    with FakePortal(state) as site:
        booking_settings = _settings(make_settings, chrome_paths, base_url=site.base_url).booking
        with BrowserManager(booking_settings) as (driver, waiter):
            AuthService(driver, waiter, booking_settings, settings.credentials).login()
            navigation = NavigationService(driver, waiter, booking_settings)
            navigation.open_booking_grid()
            assert navigation.layout == "portal"
            service = PortalBookingService(driver, waiter, booking_settings)

            assert service.try_select_date(target) is None  # day 8 not released yet

            threading.Timer(2.0, lambda: setattr(state, "released", True)).start()
            started = time.monotonic()
            service.await_date(target, timeout=30)
            assert time.monotonic() - started < 25

            result = service.sprint(service.build_plan(target), deadline=waiter.now() + 6)
    assert result.booked is True, result.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "7:00 PM", "duration": "60"}]


@pytest.mark.e2e
def test_a_quiet_close_after_confirm_is_a_booking_and_is_recorded(make_settings, chrome_paths, tmp_path):
    """What the live portal did on the first real booking: no toast, form closes."""
    from pickletime.ledger import BookingLedger

    settings = _settings(make_settings, chrome_paths)
    target = settings.booking.target_date()
    state = PortalState(today=target - timedelta(days=8), silent_success=True,
                        open_slots={target: {"6:30 PM": 2, "7:00 PM": 1}})
    ledger = BookingLedger(tmp_path / "ledger.json")
    with FakePortal(state) as site:
        bot_settings = _settings(make_settings, chrome_paths, base_url=site.base_url)
        report = CourtBookingBot(bot_settings, ledger=ledger).run()
        assert report.booked is True, report.detail
        assert report.confirmation == "inferred"
        # Recorded, so a second run for the date books nothing more.
        again = CourtBookingBot(bot_settings, ledger=ledger).run()
    assert again.skipped_reason == "already booked (ledger)"
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]


@pytest.mark.e2e
def test_a_close_that_navigates_away_stays_unverified(make_settings, chrome_paths):
    report, state, _ = _run(make_settings, chrome_paths, state_kwargs={"navigate_after_confirm": True})
    assert report.booked is False
    assert report.confirmation == "unverified"
    assert len(state.bookings) == 1  # the site did book; the bot says it cannot tell, and stops


# -- after Confirm: the first live run's open questions ---------------------------
def _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch, **state_kwargs):
    monkeypatch.setenv("PT_LOGS", str(tmp_path))
    target = _settings(make_settings, chrome_paths).booking.target_date()
    # 60 minutes at 6:30 needs the 7:00 half hour open as well.
    kwargs = {"open_slots": {target: {"6:30 PM": 1, "7:00 PM": 1}}, **state_kwargs}
    report, state, _ = _run(make_settings, chrome_paths, state_kwargs=kwargs)
    return report, state, target


@pytest.mark.e2e
def test_a_lost_confirm_press_is_pressed_again_and_books_once(make_settings, chrome_paths, tmp_path, monkeypatch):
    report, state, target = _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch,
                                      confirm_mode="needs_second_click")
    assert report.booked is True, report.detail
    assert state.book_requests == 1
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]


@pytest.mark.e2e
def test_a_busy_form_is_waited_for_past_the_old_timeout(make_settings, chrome_paths, tmp_path, monkeypatch):
    """The noon rush: the site takes longer than 15s to answer; giving up early lost the court."""
    report, state, target = _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch,
                                      confirm_mode="busy", busy_ms=14000)
    assert report.booked is True, report.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]


@pytest.mark.e2e
def test_an_unknown_second_dialog_is_never_clicked(make_settings, chrome_paths, tmp_path, monkeypatch):
    report, state, _ = _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch, confirm_mode="extra_dialog")
    assert report.booked is False
    assert report.confirmation == "unverified"
    assert "another dialog" in (report.detail or "")
    assert "OK" in (report.detail or "") and "Cancel" in (report.detail or "")
    assert state.bookings == []
    assert list(tmp_path.glob("*after-confirm*second-dialog.png"))


@pytest.mark.e2e
def test_a_confirm_that_never_responds_is_refused_with_a_screenshot(make_settings, chrome_paths, tmp_path, monkeypatch):
    report, state, _ = _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch, confirm_mode="noop")
    assert report.booked is False
    assert state.bookings == []
    assert list(tmp_path.glob("*after-confirm*form-still-open.png"))


@pytest.mark.e2e
def test_a_slow_silent_request_is_waited_for_and_never_pressed_twice(make_settings, chrome_paths, tmp_path, monkeypatch):
    """The booking request is on its way but the page shows nothing: pressing again could double-book."""
    report, state, target = _one_slot(make_settings, chrome_paths, tmp_path, monkeypatch, book_delay_ms=9000)
    assert report.booked is True, report.detail
    assert state.book_requests == 1, "Confirm was pressed again while the first request was on its way"
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]


@pytest.mark.e2e
def test_a_length_that_cannot_fit_is_skipped_without_opening_the_form(make_settings, chrome_paths):
    """60 min at 6:30 needs 7:00 open too. The live run lost a minute opening forms like this."""
    target = _settings(make_settings, chrome_paths).booking.target_date()
    report, state, _ = _run(make_settings, chrome_paths,
                            state_kwargs={"open_slots": {target: {"6:30 PM": 1, "7:30 PM": 1}}})
    assert report.booked is True, report.detail
    assert state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "30"}]
    assert state.forms_opened == ["6:30 PM"]  # opened once, to book; never for a 60 that could not fit
