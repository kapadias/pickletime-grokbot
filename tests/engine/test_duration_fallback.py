"""Duration selection when Kendo's dataSource does not match.

Reproduces the failure from the first real run against CourtReserve: the site
has Kendo, so the widget path ran, found no item whose text matched "1 hour",
and returned early — never trying the rendered list and never logging what the
dropdown actually contained. Every slot reported "60min is not offered".
"""

from __future__ import annotations

from datetime import date

import pytest
from fakes import FakeClock, FakeDriver

from pickletime.core.waits import Waiter
from pickletime.services.booking import BookingService, SlotOutcome
from pickletime.settings import BookingSettings

TARGET = date(2025, 7, 22)


def build(scripts: dict[str, object], **settings_overrides) -> tuple[BookingService, FakeDriver]:
    driver = FakeDriver({}, scripts=scripts)
    clock = FakeClock()
    waiter = Waiter(driver, default_timeout=0.5, poll_interval=0.02,
                    clock=clock.now, sleep=clock.sleep)
    defaults: dict[str, object] = {
        "webdriver_timeout": 1,
        "poll_interval_ms": 20,
        "search_start_time": "10:00 AM",
        "search_end_time": "10:00 AM",
        "preferred_durations": [60],
    }
    defaults.update(settings_overrides)
    return BookingService(driver, waiter, BookingSettings(**defaults)), driver


# Keyed by a substring of the JavaScript *source*, so selectors that travel as
# arguments cannot be used as needles.
BASE_SCRIPTS: dict[str, object] = {
    "links[i].getAttribute('data-href')": "clicked",          # open the slot
    "modal-present": True,                                    # modal is present
    "duration-control-present": True,                         # it has a duration control
    "duration-count": 2,                                      # already populated
    "dropdown.click()": "opened",                             # dropdown opens
    "box.checked": "checked",                                 # disclosure ticked
    "nodes[i]": {"status": "clicked", "text": "Save"},        # save clicked
    "successRe": {"state": "success", "text": "Your reservation is confirmed"},
}


def test_widget_miss_falls_through_to_the_dom_list() -> None:
    """A dataSource field-name miss must not veto what the user can see."""
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "not-in-list", "options": ["60 Minutes"]}
    scripts["options[i].querySelector"] = {"status": "clicked", "seen": ["1 hour"]}
    scripts["el ? (el.textContent"] = "1 hour"

    booking, driver = build(scripts)
    candidate = booking.build_plan(TARGET)[0]

    assert booking.attempt(candidate) is SlotOutcome.BOOKED
    assert driver.ran("options[i].querySelector"), "DOM fallback should have been tried"


def test_options_the_site_offers_are_reported(captured_logs) -> None:
    """The log must name the real options, not just say 'not offered'."""
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "not-in-list", "options": ["60 Minutes", "90 Minutes"]}
    scripts["options[i].querySelector"] = {"status": "missing", "seen": ["60 Minutes", "90 Minutes"]}

    booking, _ = build(scripts)
    candidate = booking.build_plan(TARGET)[0]

    outcome = booking.attempt(candidate)

    assert outcome is SlotOutcome.DURATION_UNAVAILABLE
    assert "60 Minutes" in captured_logs.text
    assert "duration_labels" in captured_logs.text  # points at the actual fix


def test_custom_duration_labels_resolve_the_mismatch() -> None:
    """The documented fix must actually work."""
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "selected", "options": ["60 Minutes"]}
    scripts["el ? (el.textContent"] = "60 Minutes"

    booking, _ = build(scripts, duration_labels={60: "60 Minutes"})
    candidate = booking.build_plan(TARGET)[0]

    assert booking.attempt(candidate) is SlotOutcome.BOOKED


def test_missing_save_button_reports_page_buttons(captured_logs) -> None:
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "selected", "options": ["1 hour"]}
    scripts["el ? (el.textContent"] = "1 hour"
    scripts["nodes[i]"] = {"status": "missing", "seen": ["Reserve Court", "Cancel"], "scope": "page"}

    booking, _ = build(scripts)
    candidate = booking.build_plan(TARGET)[0]

    assert booking.attempt(candidate) is SlotOutcome.ERROR

    assert "Reserve Court" in captured_logs.text
    assert "locators.py" in captured_logs.text


# -- the sprint must not hammer an impossible slot --------------------------
def test_a_duration_the_site_lacks_is_tried_once_per_sprint() -> None:
    """The first real run opened the same doomed modal 14 times in 30 seconds."""
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "not-in-list", "options": ["90 Minutes"]}
    scripts["options[i].querySelector"] = {"status": "missing", "seen": ["90 Minutes"]}
    scripts["kendoScheduler"] = "refreshed"
    scripts['CreateReservation"]\').length'] = 3
    scripts["toggle.click()"] = "opened"
    scripts["days[i].getAttribute('title')"] = "clicked"

    booking, _ = build(scripts)
    booking.court_date = TARGET
    plan = booking.build_plan(TARGET)

    result = booking.sprint(plan, deadline=booking.waiter.now() + 5)

    assert result.booked is False
    assert result.attempts == len(plan), "each impossible pair should be tried exactly once"
    assert "unbookable" in (result.detail or "")


def test_a_missing_slot_is_retried_because_it_may_appear() -> None:
    """The release-time case: absent now, present a moment later."""
    scripts = dict(BASE_SCRIPTS)
    scripts["links[i].getAttribute('data-href')"] = "missing"
    scripts["kendoScheduler"] = "refreshed"
    scripts['CreateReservation"]\').length'] = 3
    scripts["toggle.click()"] = "opened"
    scripts["days[i].getAttribute('title')"] = "clicked"

    booking, _ = build(scripts)
    booking.court_date = TARGET
    plan = booking.build_plan(TARGET)

    result = booking.sprint(plan, deadline=booking.waiter.now() + 1)

    assert result.attempts > len(plan), "an absent slot must be re-checked on later passes"


def test_repeated_errors_eventually_give_up() -> None:
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": "selected", "options": ["1 hour"]}
    scripts["el ? (el.textContent"] = "1 hour"
    scripts["nodes[i]"] = {"status": "missing", "seen": []}
    scripts["kendoScheduler"] = "refreshed"
    scripts['CreateReservation"]\').length'] = 3
    scripts["toggle.click()"] = "opened"
    scripts["days[i].getAttribute('title')"] = "clicked"

    booking, _ = build(scripts)
    booking.court_date = TARGET
    plan = booking.build_plan(TARGET)

    result = booking.sprint(plan, deadline=booking.waiter.now() + 10)
    assert result.attempts == 3 * len(plan), "three strikes per pair, then stop"


@pytest.mark.parametrize("status", ["no-jquery", "no-widget"])
def test_absent_kendo_still_uses_the_dom(status: str) -> None:
    scripts = dict(BASE_SCRIPTS)
    scripts["widget.dataSource"] = {"status": status, "options": []}
    scripts["options[i].querySelector"] = {"status": "clicked", "seen": ["1 hour"]}
    scripts["el ? (el.textContent"] = "1 hour"

    booking, driver = build(scripts)
    assert booking.attempt(booking.build_plan(TARGET)[0]) is SlotOutcome.BOOKED
    assert driver.ran("options[i].querySelector")


# -- the live modal: header Save button, lazily-filled duration list ----------
def test_save_button_is_found_in_the_modal_header() -> None:
    """CourtReserve puts Save in `.modal-title-buttons`, not a `.modal-footer`.

    The footer-only selector matched nothing on the live site and the run
    reported "no Save button in the modal (saw [])".
    """
    from pickletime.core import locators

    assert "[data-testid='save-btn']" in locators.SAVE_BUTTON
    assert ".modal-title-buttons button" in locators.SAVE_BUTTON
    assert "button.btn-submit" in locators.SAVE_BUTTON


def test_fixed_duration_modal_books_when_the_length_matches() -> None:
    """Some reservation types offer no duration choice at all."""
    scripts = dict(BASE_SCRIPTS)
    scripts["duration-count"] = 0                 # no rendered options
    scripts["duration-control-present"] = False   # and no control at all

    booking, driver = build(scripts, preferred_durations=[30], slot_length_minutes=30)
    assert booking.attempt(booking.build_plan(TARGET)[0]) is SlotOutcome.BOOKED
    assert not driver.ran("widget.dataSource"), "must not try to pick a duration"


def test_fixed_duration_modal_refuses_a_different_length() -> None:
    """Booking 60min into a fixed 30min slot would be the old wrong-duration bug."""
    scripts = dict(BASE_SCRIPTS)
    scripts["duration-count"] = 0
    scripts["duration-control-present"] = False

    booking, _ = build(scripts, preferred_durations=[60], slot_length_minutes=30)
    assert booking.attempt(booking.build_plan(TARGET)[0]) is SlotOutcome.DURATION_UNAVAILABLE
