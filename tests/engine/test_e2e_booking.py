"""End-to-end tests: the real bot, a real browser, a stand-in CourtReserve.

These are the tests that would have caught the three bugs the rewrite fixes, so
they assert on what the *site* recorded, not just on what the bot reported:

* ``state.bookings`` proves which duration was actually submitted — the old
  ``setTimeout`` race would show up here as ``"30 minutes"`` when 60 was asked
  for, while the bot happily reported success.
* a run where nothing is bookable must report ``booked=False``; the old
  "no Save button means it worked" heuristic reported success.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fake_site import FakeCourtReserve, SiteState

from pickletime.bot import CourtBookingBot
from pickletime.ledger import NullLedger
from pickletime.timeutil import format_long_date, format_us_date

pytestmark = pytest.mark.e2e


def _day_titles(target) -> list[str]:
    """Calendar cells around the target date."""
    return [format_long_date(target + timedelta(days=offset)) for offset in (-1, 0, 1)]


def _run(make_settings, state: SiteState, **overrides):
    settings = make_settings(**overrides)
    target = settings.booking.target_date()

    with FakeCourtReserve(state, _day_titles(target), format_us_date(target)) as site:
        settings = make_settings(base_url=site.base_url, **overrides)
        report = CourtBookingBot(settings, ledger=NullLedger()).run()
    return report, target


@pytest.fixture(name="_wire_chrome")
def wire_chrome(chrome_paths, make_settings):
    """A settings factory pinned to whichever local browser we found."""
    binary, driver = chrome_paths

    def factory(**overrides):
        overrides.setdefault("chrome_binary", binary)
        if driver:
            overrides.setdefault("chrome_driver_path", driver)
        return make_settings(**overrides)

    return factory


def test_books_the_priority_slot_at_the_preferred_duration(_wire_chrome):
    state = SiteState(
        slots={
            "10:00 AM": ["30 minutes"],
            "11:30 AM": ["30 minutes", "1 hour"],
        }
    )
    report, target = _run(_wire_chrome, state)

    assert report.booked is True
    assert report.error is None
    assert report.slot_time == "11:30 AM"
    assert report.duration == 60
    assert report.confirmation == "explicit"
    assert report.court_date == format_us_date(target)

    # What the *site* recorded. This is the assertion the old duration race fails.
    assert state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]


def test_falls_back_to_the_shorter_duration(_wire_chrome):
    state = SiteState(slots={"10:30 AM": ["30 minutes"]})
    report, _ = _run(_wire_chrome, state)

    assert report.booked is True
    assert report.duration == 30
    assert state.bookings == [{"slot": "10:30 AM", "duration": "30 minutes"}]


def test_honours_time_priority_mode(_wire_chrome):
    state = SiteState(slots={"10:00 AM": ["30 minutes", "1 hour"], "11:30 AM": ["30 minutes"]})
    report, _ = _run(
        _wire_chrome,
        state,
        booking_priority="time",
        priority_time_slots=["11:30 AM", "10:00 AM"],
    )

    # "time" mode exhausts every duration at the best slot before moving on, so
    # 11:30 AM at 30 minutes beats 10:00 AM at 60.
    assert report.booked is True
    assert state.bookings == [{"slot": "11:30 AM", "duration": "30 minutes"}]


def test_empty_calendar_reports_failure_not_success(_wire_chrome):
    state = SiteState(slots={})
    report, _ = _run(_wire_chrome, state)

    assert report.booked is False
    assert report.error is None
    assert state.bookings == []
    assert report.unavailable > 0


def test_site_rejection_is_not_reported_as_booked(_wire_chrome):
    state = SiteState(slots={"11:30 AM": ["1 hour"]}, reject_save=True)
    report, _ = _run(_wire_chrome, state)

    assert report.booked is False
    assert state.bookings == []
    assert report.rejected > 0


def test_a_save_the_site_never_confirms_is_reported_as_unverified(_wire_chrome):
    """A closed modal is not a reservation, and this proves it cannot be read as one.

    Here the site *did* record the booking while saying nothing about it. On the
    real site the identical sequence — modal closes, no message — recorded
    nothing, and the bot called it a success, sent the "court booked" SMS and
    wrote the ledger for a court that did not exist. The two cases are
    indistinguishable from the modal alone, so the only honest answer is that we
    do not know.
    """
    state = SiteState(slots={"11:30 AM": ["1 hour"]}, silent_success=True)
    report, _ = _run(_wire_chrome, state)

    assert report.booked is False, "an unconfirmed save must never be reported as booked"
    assert report.confirmation == "unverified"
    assert report.slot_time == "11:30 AM", "still say which slot is in doubt"
    assert "without a verdict" in (report.detail or ""), report.detail
    # The ambiguity is genuine: the site really did save here.
    assert state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]


def test_an_unverified_save_stops_the_sprint(_wire_chrome):
    """Carrying on could book a second court on the same evening."""
    state = SiteState(
        slots={"11:30 AM": ["1 hour"], "10:30 AM": ["1 hour"], "10:00 AM": ["1 hour"]},
        silent_success=True,
    )
    report, _ = _run(_wire_chrome, state)

    assert report.confirmation == "unverified"
    assert len(state.bookings) == 1, f"must not keep booking after an unclear save: {state.bookings}"


def test_dry_run_never_saves(_wire_chrome):
    state = SiteState(slots={"11:30 AM": ["1 hour"]})
    report, _ = _run(_wire_chrome, state, dry_run=True)

    assert report.booked is False
    assert report.dry_run is True
    assert report.confirmation == "dry-run"
    assert report.slot_time == "11:30 AM"
    assert state.bookings == []


def test_sprint_rewalks_until_slots_appear(_wire_chrome):
    """Slots that are not on the grid yet at T0 must still be caught.

    This is the release-race case: the first pass sees an empty calendar because
    the release has not committed, and the slot shows up a moment later. The old
    bot made exactly one pass and gave up.
    """
    state = SiteState(slots={"11:30 AM": ["1 hour"]}, reveal_after_loads=1)
    report, _ = _run(_wire_chrome, state, sprint_seconds=20)

    assert report.booked is True
    assert state.grid_loads > 1, "expected the sprint to re-read the grid"
    assert state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]


def test_kendo_datasource_miss_still_books_via_the_dom(_wire_chrome):
    """The failure from the first real run against CourtReserve.

    The site has Kendo, so the widget path runs. Its dataSource items use a field
    name the widget path cannot read, so no item matches "1 hour". The rendered
    <li> list is correct, and the booking must still succeed — the widget miss
    must not veto the DOM fallback.
    """
    state = SiteState(slots={"11:30 AM": ["30 minutes", "1 hour"]}, kendo_shim=True)
    report, _ = _run(_wire_chrome, state)

    assert report.booked is True
    assert report.duration == 60
    assert state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]


def test_unknown_duration_wording_is_reported_with_the_real_options(
    _wire_chrome, captured_logs
):
    """When the site's wording differs, the log must name what it offers."""
    state = SiteState(slots={"11:30 AM": ["60 Minutes", "90 Minutes"]})
    report, _ = _run(_wire_chrome, state)

    assert report.booked is False
    assert "60 Minutes" in captured_logs.text
    assert "duration_labels" in captured_logs.text


def test_configured_duration_labels_book_against_custom_wording(_wire_chrome):
    """And applying the documented fix makes it work."""
    state = SiteState(slots={"11:30 AM": ["60 Minutes"]})
    report, _ = _run(
        _wire_chrome, state, preferred_durations=[60], duration_labels={60: "60 Minutes"}
    )

    assert report.booked is True
    assert state.bookings == [{"slot": "11:30 AM", "duration": "60 Minutes"}]


def test_waits_for_a_lazily_populated_duration_list(_wire_chrome):
    """The live modal renders its shell first and fills the dropdown after.

    Acting on the half-built form is why the same slot alternated between
    "duration not offered" and "no Save button" across passes.
    """
    state = SiteState(slots={"11:30 AM": ["1 hour"]}, duration_delay_ms=900)
    report, _ = _run(_wire_chrome, state)

    assert report.booked is True
    assert state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]


def test_bad_credentials_fail_fast(_wire_chrome):
    state = SiteState(password="something-else", slots={"11:30 AM": ["1 hour"]})
    report, _ = _run(_wire_chrome, state)

    assert report.booked is False
    assert report.error is not None
    assert "LoginError" in report.error
    assert state.login_attempts == ["player@example.com"]


def test_chrome_does_not_open_the_new_tab_page(chrome_paths):
    """The startup page, asserted against a real browser.

    A previous fix for this passed a bare "about:blank" in the argument list and
    was completely inert — chromedriver turns it into the switch
    "--about:blank", which Chrome discards. The test that accompanied it checked
    Selenium's argument list, so it passed while the browser still opened the
    New Tab Page in production. Only the browser can answer this.
    """
    from pickletime.core.browser import BrowserManager
    from pickletime.settings import BookingSettings

    binary, driver = chrome_paths
    settings = BookingSettings(
        headless=True, chrome_binary=binary, chrome_driver_path=driver
    )
    manager = BrowserManager(settings)
    try:
        webdriver, _ = manager.start()
        landed = webdriver.current_url
    finally:
        manager.close()

    assert "new-tab-page" not in landed, (
        f"Chrome opened {landed}; the NTP pulls in the WebUI toolbar and a live "
        "call to accounts.google.com before the bot asks for anything"
    )
    assert landed in {"about:blank", "data:,"}, landed
