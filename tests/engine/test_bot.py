"""Orchestrator behaviour: retries, terminal failures, arming, and the ledger."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pytest

import pickletime.bot as bot_module
from pickletime.bot import CourtBookingBot
from pickletime.errors import BrowserStartError, LoginError
from pickletime.ledger import BookingLedger
from pickletime.services.booking import BookingResult
from pickletime.settings import BookingSettings, Credentials, RuntimeSettings, Settings
from pickletime.timeutil import get_timezone

PACIFIC = get_timezone("America/Los_Angeles")


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def success(self, details): self.events.append(("success", details))

    def failure(self, reason, details=None): self.events.append(("failure", reason))

    def error(self, message, details=None): self.events.append(("error", message))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class FakeBrowser:
    """Context manager yielding placeholder (driver, waiter)."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return ("driver", FakeWaiter())

    def __exit__(self, *_exc) -> None:
        self.exited += 1


class FakeWaiter:
    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        self._t += 0.01
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


def settings_for(tmp_path, **overrides) -> Settings:
    defaults: dict[str, Any] = {
        "search_start_time": "10:00 AM",
        "search_end_time": "11:00 AM",
        "preferred_durations": [60],
        "max_retries": 0,
        "retry_interval": 0,
        "sprint_seconds": 1,
    }
    defaults.update(overrides)
    return Settings(
        booking=BookingSettings(**defaults),
        credentials=Credentials(username="player@example.com", password="correct-horse"),
        runtime=RuntimeSettings(ledger_path=str(tmp_path / "ledger.json")),
    )


def install_services(
    monkeypatch,
    *,
    login=None,
    navigate=None,
    select_date=None,
    sprint_result=None,
    sprint_raises=None,
    refreshes=None,
    date_opens_at_release=False,
    awaited=None,
):
    """Swap the three services for stubs so no browser is needed."""

    class Auth:
        def __init__(self, *_a): ...
        def login(self) -> float:
            if login:
                login()
            return 0.1

    class Nav:
        def __init__(self, *_a): ...
        def open_booking_grid(self) -> float:
            if navigate:
                navigate()
            return 0.1

    class Booking:
        def __init__(self, *_a):
            self.court_date = None

        def select_date(self, target) -> float:
            self.court_date = target
            if select_date:
                select_date()
            return 0.1

        def try_select_date(self, target):
            """None models a date the facility has not released yet."""
            if date_opens_at_release:
                return None
            return self.select_date(target)

        def await_date(self, target, *, timeout=45.0) -> float:
            if awaited is not None:
                awaited.append(target)
            self.court_date = target
            return 0.2

        def build_plan(self, target):
            return ["candidate"]

        def refresh_grid(self):
            if refreshes is not None:
                refreshes.append(True)
            return "refreshed"

        def sprint(self, plan, *, deadline, refresh_between_passes=True):
            if sprint_raises:
                raise sprint_raises
            return sprint_result or BookingResult(booked=False, court_date="7/22/2025")

    monkeypatch.setattr(bot_module, "AuthService", Auth)
    monkeypatch.setattr(bot_module, "NavigationService", Nav)
    monkeypatch.setattr(bot_module, "BookingService", Booking)


# -- happy path -------------------------------------------------------------
def test_successful_run_records_the_ledger_and_notifies(tmp_path, monkeypatch) -> None:
    install_services(
        monkeypatch,
        sprint_result=BookingResult(
            booked=True, court_date="7/22/2025", slot_time="10:30 AM",
            duration=60, confirmation="explicit", attempts=2,
        ),
    )
    settings = settings_for(tmp_path)
    notifier = RecordingNotifier()
    browser = FakeBrowser()

    report = CourtBookingBot(
        settings, notifier=notifier, browser_factory=lambda: browser
    ).run()

    assert report.booked is True
    assert report.slot_time == "10:30 AM"
    assert notifier.kinds() == ["success"]
    assert browser.entered == 1 and browser.exited == 1

    key = BookingLedger.key_for(13233, 16984, "7/22/2025")
    assert BookingLedger(tmp_path / "ledger.json").lookup(key) is not None


def test_run_reports_phase_timings(tmp_path, monkeypatch) -> None:
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="7/22/2025"))
    report = CourtBookingBot(
        settings_for(tmp_path), notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    assert {"chrome", "login", "navigate", "select_date", "sprint", "total"} <= set(report.timings)


# -- no slots ---------------------------------------------------------------
def test_empty_calendar_notifies_failure_and_leaves_the_ledger_alone(tmp_path, monkeypatch) -> None:
    install_services(
        monkeypatch,
        sprint_result=BookingResult(
            booked=False, court_date="7/22/2025", unavailable=5, detail="No slot won"
        ),
    )
    notifier = RecordingNotifier()
    report = CourtBookingBot(
        settings_for(tmp_path), notifier=notifier, browser_factory=FakeBrowser
    ).run()

    assert report.booked is False
    assert report.error is None  # a business outcome, not a fault
    assert notifier.kinds() == ["failure"]

    key = BookingLedger.key_for(13233, 16984, "7/22/2025")
    assert BookingLedger(tmp_path / "ledger.json").lookup(key) is None


# -- idempotency ------------------------------------------------------------
def test_already_booked_date_short_circuits_before_starting_chrome(tmp_path, monkeypatch) -> None:
    """A scheduler retry must not book a second court."""
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="7/22/2025"))
    settings = settings_for(tmp_path, court_date="07/22/2025")

    ledger = BookingLedger(tmp_path / "ledger.json")
    ledger.record(
        BookingLedger.key_for(13233, 16984, "7/22/2025"),
        {"time": "10:30 AM", "duration": 60, "confirmation": "explicit"},
    )

    browser = FakeBrowser()
    notifier = RecordingNotifier()
    report = CourtBookingBot(
        settings, notifier=notifier, ledger=ledger, browser_factory=lambda: browser
    ).run()

    assert report.booked is True
    assert report.skipped_reason == "already booked (ledger)"
    assert report.slot_time == "10:30 AM"
    assert browser.entered == 0, "must not start a browser for an already-booked date"
    assert notifier.kinds() == []


# -- rotation ---------------------------------------------------------------
ROTATION = {"anchor_date": "08/24/2026", "cycle_days": 14, "play_on_offsets": [3, 7, 11]}


def test_a_date_outside_the_rotation_skips_before_starting_chrome(tmp_path, monkeypatch) -> None:
    """The scheduler fires daily; most firings must cost nothing."""
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="8/26/2026"))
    settings = settings_for(tmp_path, court_date="08/26/2026", rotation=ROTATION)

    browser = FakeBrowser()
    notifier = RecordingNotifier()
    report = CourtBookingBot(
        settings, notifier=notifier, browser_factory=lambda: browser
    ).run()

    assert report.skipped_reason == "outside the rotation"
    assert report.booked is False
    assert report.error is None, "a day off is not a fault"
    assert browser.entered == 0
    assert notifier.kinds() == [], "no alert for a day we were never going to play"
    assert "cycle day 2" in (report.detail or "")


def test_a_date_inside_the_rotation_runs_normally(tmp_path, monkeypatch) -> None:
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="8/27/2026"))
    settings = settings_for(tmp_path, court_date="08/27/2026", rotation=ROTATION)

    browser = FakeBrowser()
    report = CourtBookingBot(settings, notifier=RecordingNotifier(),
                             browser_factory=lambda: browser).run()

    assert report.skipped_reason is None
    assert report.booked is True
    assert browser.entered == 1


def test_without_a_rotation_every_day_is_bookable(tmp_path, monkeypatch) -> None:
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="8/26/2026"))
    settings = settings_for(tmp_path, court_date="08/26/2026")

    report = CourtBookingBot(
        settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    assert report.skipped_reason is None
    assert report.booked is True


def test_the_rotation_is_checked_before_the_ledger(tmp_path, monkeypatch) -> None:
    """A ledger entry for a non-play date must not mask why nothing ran."""
    install_services(monkeypatch, sprint_result=BookingResult(booked=True, court_date="8/26/2026"))
    settings = settings_for(tmp_path, court_date="08/26/2026", rotation=ROTATION)

    ledger = BookingLedger(tmp_path / "ledger.json")
    ledger.record(
        BookingLedger.key_for(13233, 16984, "8/26/2026"),
        {"time": "10:30 AM", "duration": 60, "confirmation": "explicit"},
    )

    report = CourtBookingBot(
        settings, notifier=RecordingNotifier(), ledger=ledger, browser_factory=FakeBrowser
    ).run()

    assert report.skipped_reason == "outside the rotation"


# -- failure handling -------------------------------------------------------
def test_login_failure_is_terminal_and_not_retried(tmp_path, monkeypatch) -> None:
    attempts: list[int] = []

    def failing_login() -> None:
        attempts.append(1)
        raise LoginError("Login rejected by the site: Invalid email or password.")

    install_services(monkeypatch, login=failing_login)
    notifier = RecordingNotifier()

    report = CourtBookingBot(
        settings_for(tmp_path, max_retries=3),
        notifier=notifier,
        browser_factory=FakeBrowser,
    ).run()

    assert report.booked is False
    assert "LoginError" in (report.error or "")
    assert len(attempts) == 1, "bad credentials will not become good on a retry"
    assert notifier.kinds() == ["error"]


def test_infrastructure_failure_is_retried_then_reported(tmp_path, monkeypatch) -> None:
    attempts: list[int] = []

    def failing_nav() -> None:
        attempts.append(1)
        raise BrowserStartError("chrome died")

    install_services(monkeypatch, navigate=failing_nav)
    notifier = RecordingNotifier()

    report = CourtBookingBot(
        settings_for(tmp_path, max_retries=2, retry_interval=0),
        notifier=notifier,
        browser_factory=FakeBrowser,
    ).run()

    assert len(attempts) == 3  # initial attempt + 2 retries
    assert report.retries_used == 2
    assert "BrowserStartError" in (report.error or "")
    assert notifier.kinds() == ["error"]


def test_a_retry_can_succeed(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrowserStartError("first attempt died")

    install_services(
        monkeypatch, navigate=flaky,
        sprint_result=BookingResult(booked=True, court_date="7/22/2025", slot_time="10:00 AM"),
    )
    report = CourtBookingBot(
        settings_for(tmp_path, max_retries=1, retry_interval=0),
        notifier=RecordingNotifier(),
        browser_factory=FakeBrowser,
    ).run()

    assert report.booked is True
    assert report.retries_used == 1


# -- dry run ----------------------------------------------------------------
def test_dry_run_neither_records_nor_notifies_success(tmp_path, monkeypatch) -> None:
    install_services(
        monkeypatch,
        sprint_result=BookingResult(
            booked=False, court_date="7/22/2025", dry_run=True,
            slot_time="10:30 AM", confirmation="dry-run",
        ),
    )
    notifier = RecordingNotifier()
    report = CourtBookingBot(
        settings_for(tmp_path, dry_run=True), notifier=notifier, browser_factory=FakeBrowser
    ).run()

    assert report.dry_run is True
    assert notifier.kinds() == []
    key = BookingLedger.key_for(13233, 16984, "7/22/2025")
    assert BookingLedger(tmp_path / "ledger.json").lookup(key) is None


# -- arming -----------------------------------------------------------------
def test_arm_waits_for_the_release_moment(tmp_path, monkeypatch) -> None:
    settings = settings_for(tmp_path, release_time="12:00 PM", max_arm_wait_seconds=300)
    bot = CourtBookingBot(settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser)

    monkeypatch.setattr(bot_module, "now_in", lambda tz: datetime(2025, 7, 10, 11, 57, 30, tzinfo=PACIFIC))
    slept: list[Any] = []
    monkeypatch.setattr(bot_module, "sleep_until", lambda target, **kw: slept.append(target) or 150.0)

    waited = bot._arm()

    assert waited == 150.0
    assert slept[0] == datetime(2025, 7, 10, 12, 0, tzinfo=PACIFIC)


def test_arm_is_a_no_op_without_a_release_time(tmp_path) -> None:
    bot = CourtBookingBot(
        settings_for(tmp_path), notifier=RecordingNotifier(), browser_factory=FakeBrowser
    )
    assert bot._arm() == 0.0


def test_arm_sprints_immediately_when_release_already_passed(tmp_path, monkeypatch) -> None:
    settings = settings_for(tmp_path, release_time="12:00 PM")
    bot = CourtBookingBot(settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser)

    # 30s late: within tolerance, so "next occurrence" is today's, already gone.
    monkeypatch.setattr(
        bot_module, "now_in", lambda tz: datetime(2025, 7, 10, 12, 0, 30, tzinfo=PACIFIC)
    )
    assert bot._arm() == 0.0


def test_arm_refuses_to_park_the_request_for_hours(tmp_path, monkeypatch) -> None:
    """A misconfigured release time must not hold a Cloud Run request open."""
    settings = settings_for(tmp_path, release_time="12:00 PM", max_arm_wait_seconds=300)
    bot = CourtBookingBot(settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser)

    monkeypatch.setattr(
        bot_module, "now_in", lambda tz: datetime(2025, 7, 10, 13, 0, tzinfo=PACIFIC)
    )

    def explode(*_a, **_k):
        raise AssertionError("must not sleep for ~23 hours")

    monkeypatch.setattr(bot_module, "sleep_until", explode)

    assert bot._arm() == 0.0


def test_release_time_parses_from_config() -> None:
    assert BookingSettings(release_time="12:00 PM").release_time == time(12, 0)


@pytest.mark.parametrize("bad", ["25:00 PM", "noon"])
def test_bad_release_time_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        BookingSettings(release_time=bad)


# -- the date is not released until the release moment ----------------------
def test_a_date_not_yet_in_the_picker_waits_for_release_instead_of_failing(
    tmp_path, monkeypatch
) -> None:
    """The failure that killed the first real scheduled run.

    The facility adds the Nth day out at the release moment, so at 11:57 the
    target date does not exist. Warm-up treated that as an error, both attempts
    burned in seconds, and the run died three minutes before the date it was
    waiting for — the warm-up lead turned into the cause of failure.
    """
    awaited: list = []
    install_services(
        monkeypatch,
        date_opens_at_release=True,
        awaited=awaited,
        sprint_result=BookingResult(booked=True, court_date="9/10/2026", slot_time="6:30 PM"),
    )
    settings = settings_for(tmp_path, court_date="09/10/2026", release_time="12:00 PM")

    # Arm resolves against the wall clock; stub it so the test does not sleep.
    monkeypatch.setattr(CourtBookingBot, "_arm", lambda self: 0.0)
    monkeypatch.setattr(CourtBookingBot, "_seconds_until_release", lambda self: 180.0)

    report = CourtBookingBot(
        settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    assert report.error is None, report.error
    assert report.booked is True
    assert awaited == [settings.booking.target_date()], "must claim the date at release"


def test_a_date_missing_with_no_release_pending_is_still_an_error(
    tmp_path, monkeypatch
) -> None:
    """Waiting is only correct when there is something to wait for.

    Otherwise days_in_advance is simply wrong for this facility, and silently
    proceeding would hide it.
    """
    install_services(monkeypatch, date_opens_at_release=True)
    settings = settings_for(tmp_path, court_date="09/10/2026")

    monkeypatch.setattr(CourtBookingBot, "_seconds_until_release", lambda self: 0.0)

    report = CourtBookingBot(
        settings, notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    assert report.booked is False
    assert report.error is not None
    assert "not selectable" in report.error


def test_a_date_already_open_still_refreshes_rather_than_reloading(
    tmp_path, monkeypatch
) -> None:
    """The pre-existing path must not pay for a full page reload it does not need."""
    refreshes: list = []
    awaited: list = []
    install_services(
        monkeypatch,
        refreshes=refreshes,
        awaited=awaited,
        sprint_result=BookingResult(booked=True, court_date="7/22/2025"),
    )
    monkeypatch.setattr(CourtBookingBot, "_arm", lambda self: 0.0)

    report = CourtBookingBot(
        settings_for(tmp_path), notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    assert report.booked is True
    assert refreshes == [True]
    assert awaited == []


# -- a save the site never confirmed ----------------------------------------
def _unverified_result() -> BookingResult:
    return BookingResult(
        booked=False,
        court_date="9/28/2026",
        slot_time="7:00 PM",
        duration=60,
        confirmation="unverified",
        detail="modal closed without a verdict",
    )


def test_an_unverified_save_does_not_report_success(tmp_path, monkeypatch) -> None:
    """The false success that cost a real booking.

    The site closed the modal without confirming, the bot called it booked, sent
    the "court booked" SMS, and there was no court.
    """
    install_services(monkeypatch, sprint_result=_unverified_result())
    notifier = RecordingNotifier()

    report = CourtBookingBot(
        settings_for(tmp_path), notifier=notifier, browser_factory=FakeBrowser
    ).run()

    assert report.booked is False
    assert report.confirmation == "unverified"
    assert notifier.kinds() == ["error"], "must not send a success alert for a maybe"


def test_an_unverified_save_still_blocks_a_second_booking(tmp_path, monkeypatch) -> None:
    """The other half: if it *did* book, a retry must not take a second court."""
    install_services(monkeypatch, sprint_result=_unverified_result())

    CourtBookingBot(
        settings_for(tmp_path), notifier=RecordingNotifier(), browser_factory=FakeBrowser
    ).run()

    key = BookingLedger.key_for(13233, 16984, "9/28/2026")
    entry = BookingLedger(tmp_path / "ledger.json").lookup(key)
    assert entry is not None, "an ambiguous save must still be recorded"
    assert entry.payload["confirmation"] == "unverified"


def test_a_retry_after_an_unverified_save_skips_rather_than_rebooking(
    tmp_path, monkeypatch
) -> None:
    install_services(monkeypatch, sprint_result=_unverified_result())
    settings = settings_for(tmp_path, court_date="09/28/2026")

    first = CourtBookingBot(settings, notifier=RecordingNotifier(),
                            browser_factory=FakeBrowser).run()
    assert first.confirmation == "unverified"

    browser = FakeBrowser()
    second = CourtBookingBot(
        settings, notifier=RecordingNotifier(), browser_factory=lambda: browser
    ).run()

    assert second.skipped_reason == "already booked (ledger)"
    assert browser.entered == 0, "the retry job must not open a browser for this date"
