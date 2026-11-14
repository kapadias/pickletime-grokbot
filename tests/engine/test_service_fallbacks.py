"""The paths that only execute when something has gone wrong."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fakes import FakeClock, FakeDriver, FakeElement
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

import pickletime.core.browser as browser_module
from pickletime.core import locators
from pickletime.core.browser import BrowserManager, chrome_version
from pickletime.core.waits import Waiter
from pickletime.errors import BookingError, BrowserStartError, LoginError, NavigationError
from pickletime.services.auth import AuthService
from pickletime.services.booking import BookingService
from pickletime.services.navigation import NavigationService
from pickletime.settings import BookingSettings, Credentials
from pickletime.timeutil import now_in

CREDS = Credentials(username="player@example.com", password="correct-horse")


def waiter_for(driver: FakeDriver, timeout: float = 1.0) -> Waiter:
    clock = FakeClock()
    return Waiter(driver, default_timeout=timeout, poll_interval=0.05,
                  clock=clock.now, sleep=clock.sleep)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
def test_direct_navigation_is_preferred() -> None:
    """One GET instead of two click-and-sleep cycles through the menu."""
    driver = FakeDriver({(By.CSS_SELECTOR, ".k-scheduler-content"): [FakeElement()]})
    settings = BookingSettings(webdriver_timeout=1)
    NavigationService(driver, waiter_for(driver), settings).open_booking_grid()

    assert driver.visited == [settings.bookings_url]


def test_menu_fallback_runs_when_the_grid_does_not_render() -> None:
    grid: list[FakeElement] = []
    reservations = FakeElement()
    sport = FakeElement()
    driver = FakeDriver(
        {
            (By.CSS_SELECTOR, ".k-scheduler-content"): grid,
            (By.XPATH, "//a[contains(@href, '/Online/Reservations/')]"): [reservations],
            ("xpath", "//a[contains(@href, 'sId=16984')]"): [sport],
        }
    )
    settings = BookingSettings(webdriver_timeout=1)
    navigation = NavigationService(driver, waiter_for(driver, 0.3), settings)

    # The grid appears only once the menu walk has clicked the sport link.
    original_click = driver.execute_script

    def click_then_render(script: str, *args: object):
        result = original_click(script, *args)
        if "arguments[0].click()" in script and args and args[0] is sport:
            grid.append(FakeElement())
        return result

    driver.execute_script = click_then_render  # type: ignore[method-assign]
    navigation.open_booking_grid()

    assert grid, "menu fallback should have reached the grid"


def test_navigation_failure_is_reported_with_context() -> None:
    driver = FakeDriver({})
    driver.get = lambda url: setattr(driver, "current_url", "https://app.courtreserve.com/Online/Portal")  # type: ignore[method-assign]
    settings = BookingSettings(webdriver_timeout=1, sport_id=4242)
    navigation = NavigationService(driver, waiter_for(driver, 0.2), settings)

    with pytest.raises(NavigationError, match="4242"):
        navigation.open_booking_grid()


def test_new_layout_bookings_page_names_the_cause_and_the_capture() -> None:
    """The real 2026-11 failure: login fine, bookings page loads, no Kendo grid.

    The old message blamed a missing menu entry, which sent the reader to the
    wrong place. It must say the layout changed and how to capture it.
    """
    driver = FakeDriver({})  # no .k-scheduler-content, no menu entry
    settings = BookingSettings(webdriver_timeout=1)
    navigation = NavigationService(driver, waiter_for(driver, 0.2), settings)

    with pytest.raises(NavigationError, match="neither the Kendo scheduler grid nor the member portal") as excinfo:
        navigation.open_booking_grid()
    assert "run.sh capture --open-slot" in str(excinfo.value)
    assert "Nothing was booked" in str(excinfo.value)


def test_landing_elsewhere_keeps_the_original_message() -> None:
    """Redirected away from the bookings page (say, to a login page): not a layout change."""
    driver = FakeDriver({})
    driver.get = lambda url: setattr(driver, "current_url", "https://app.courtreserve.com/Online/Account/LogIn/13233")  # type: ignore[method-assign]
    settings = BookingSettings(webdriver_timeout=1, sport_id=4242)
    navigation = NavigationService(driver, waiter_for(driver, 0.2), settings)

    with pytest.raises(NavigationError, match="4242"):
        navigation.open_booking_grid()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_missing_email_field_names_the_likely_cause() -> None:
    driver = FakeDriver({})
    settings = BookingSettings(webdriver_timeout=1)

    with pytest.raises(LoginError, match="layout may have changed|unreachable"):
        AuthService(driver, waiter_for(driver, 0.2), settings, CREDS).login()


def test_site_error_message_is_surfaced() -> None:
    driver = FakeDriver(
        {
            (By.ID, "UserNameOrEmail"): [FakeElement()],
            (By.ID, "Password"): [FakeElement()],
            (By.CSS_SELECTOR, ".validation-summary-errors"): [
                FakeElement(text="Invalid email or password.")
            ],
        },
        current_url="https://app.courtreserve.com/Online/Account/LogIn/13233",
    )
    settings = BookingSettings(webdriver_timeout=1)

    with pytest.raises(LoginError, match="Invalid email or password"):
        AuthService(driver, waiter_for(driver, 0.3), settings, CREDS).login()


def test_credentials_are_typed_into_the_form() -> None:
    email, password = FakeElement(), FakeElement()
    driver = FakeDriver(
        {
            (By.ID, "UserNameOrEmail"): [email],
            (By.ID, "Password"): [password],
            (By.CSS_SELECTOR, "a.parent-header-link"): [FakeElement()],
        },
        current_url="https://app.courtreserve.com/Online/Account/LogIn/13233",
    )
    AuthService(driver, waiter_for(driver, 0.5), BookingSettings(webdriver_timeout=1), CREDS).login()

    assert email.keys == ["player@example.com"]
    # With no submit button present, the form is submitted with Enter on the
    # password field, so a trailing RETURN key is expected here.
    assert password.keys[0] == "correct-horse"


def test_screenshot_is_written_only_when_configured(tmp_path: Path) -> None:
    driver = FakeDriver({})
    without = BookingSettings(webdriver_timeout=1)
    assert AuthService(driver, waiter_for(driver), without, CREDS).screenshot("x") is None

    shots = tmp_path / "shots"
    with_dir = BookingSettings(webdriver_timeout=1, screenshot_dir=str(shots))
    path = AuthService(driver, waiter_for(driver), with_dir, CREDS).screenshot("login-failed")

    assert path is not None
    assert shots.is_dir()
    assert "login-failed" in path


# ---------------------------------------------------------------------------
# Browser options
# ---------------------------------------------------------------------------
def arguments_of(settings: BookingSettings) -> list[str]:
    manager = BrowserManager(settings)
    try:
        return list(manager.build_options().arguments)
    finally:
        manager.close()


def test_same_origin_policy_is_not_disabled() -> None:
    """The bot types a real password into this browser."""
    assert "--disable-web-security" not in arguments_of(BookingSettings())


@pytest.mark.parametrize(
    "bogus",
    ["--disable-images", "--shm-size=2g", "--max_old_space_size=2048", "--no-zygote"],
)
def test_flags_that_are_not_chrome_switches_are_gone(bogus: str) -> None:
    """--disable-images is a myth, --shm-size is Docker, --max_old_space_size is Node."""
    assert not any(arg.startswith(bogus.split("=")[0]) for arg in arguments_of(BookingSettings()))


def test_headless_and_profile_isolation() -> None:
    args = arguments_of(BookingSettings(headless=True))
    assert "--headless=new" in args
    assert any(arg.startswith("--user-data-dir=") for arg in args)

    assert "--headless=new" not in arguments_of(BookingSettings(headless=False))


def test_window_size_flows_through() -> None:
    assert "--window-size=1280,720" in arguments_of(BookingSettings(window_size="1280,720"))


def test_profile_directory_is_removed_on_close() -> None:
    manager = BrowserManager(BookingSettings())
    manager.build_options()
    profile = manager._profile_dir
    assert profile and Path(profile).is_dir()

    manager.close()
    assert not Path(profile).exists()


def test_chrome_start_is_retried_then_reported(monkeypatch) -> None:
    attempts = {"n": 0}

    def always_failing(*_a, **_k):
        attempts["n"] += 1
        raise WebDriverException("chrome not reachable")

    monkeypatch.setattr(browser_module.webdriver, "Chrome", always_failing)
    monkeypatch.setattr(browser_module.time, "sleep", lambda _s: None)

    with pytest.raises(BrowserStartError, match="after 3 attempts"):
        BrowserManager(BookingSettings()).start(max_attempts=3)
    assert attempts["n"] == 3


def test_missing_chromedriver_path_falls_back_to_selenium_manager() -> None:
    manager = BrowserManager(BookingSettings(chrome_driver_path="/does/not/exist"))
    service = manager._build_service()
    assert service.path in (None, "", "chromedriver") or "does/not/exist" not in str(service.path)


def test_chrome_version_never_raises() -> None:
    assert chrome_version("/definitely/not/a/browser") is None


# ---------------------------------------------------------------------------
# Booking service edge cases
# ---------------------------------------------------------------------------
def booking_for(scripts: dict[str, object]) -> tuple[BookingService, FakeDriver]:
    driver = FakeDriver({}, scripts=scripts)
    settings = BookingSettings(webdriver_timeout=1, poll_interval_ms=20)
    return BookingService(driver, waiter_for(driver, 0.4), settings), driver


def test_select_date_pages_forward_when_the_month_is_wrong() -> None:
    """The picker can open on a month that does not contain the target date."""
    paged = {"forward": False}

    def next_month() -> str:
        paged["forward"] = True
        return "clicked"

    booking, driver = booking_for(
        {
            "toggle.click()": "opened",
            # The day is only findable once the calendar has paged forward.
            "days[i].getAttribute('title')": lambda: "clicked" if paged["forward"] else "missing",
            ".k-calendar .k-nav-next": next_month,
            "CreateReservation\"]').length": 4,
        }
    )
    booking.select_date(date(2025, 7, 22))

    assert paged["forward"] is True
    assert driver.calls_to(".k-calendar .k-nav-next") >= 1


def test_select_date_gives_up_with_a_clear_message() -> None:
    booking, _ = booking_for(
        {"toggle.click()": "opened", "days[i].getAttribute('title')": "missing",
         ".k-calendar .k-nav-next": "clicked"}
    )
    with pytest.raises(BookingError, match="Tuesday, July 22, 2025"):
        booking.select_date(date(2025, 7, 22))


def test_select_date_reports_a_missing_picker() -> None:
    booking, _ = booking_for({"toggle.click()": "missing"})
    with pytest.raises(BookingError, match="date picker"):
        booking.select_date(date(2025, 7, 22))


def test_refresh_falls_back_to_a_page_reload() -> None:
    booking, driver = booking_for(
        {
            "kendoScheduler": "no-jquery",
            "toggle.click()": "opened",
            "days[i].getAttribute('title')": "clicked",
            "CreateReservation\"]').length": 2,
        }
    )
    booking.court_date = date(2025, 7, 22)
    assert booking.refresh_grid() == "reloaded"
    assert driver.refreshes == 1


def test_unbookable_slot_returns_immediately() -> None:
    """A missing slot must cost one script call, not a modal timeout."""
    booking, driver = booking_for({"links[i].getAttribute('data-href')": "missing"})
    candidate = booking.build_plan(date(2025, 7, 22))[0]

    from pickletime.services.booking import SlotOutcome

    assert booking.attempt(candidate) is SlotOutcome.UNAVAILABLE
    assert not driver.ran(locators.DISCLOSURE_CHECKBOX)


def test_hidden_slot_is_treated_as_unavailable() -> None:
    booking, _ = booking_for({"links[i].getAttribute('data-href')": "hidden"})
    candidate = booking.build_plan(date(2025, 7, 22))[0]

    from pickletime.services.booking import SlotOutcome

    assert booking.attempt(candidate) is SlotOutcome.UNAVAILABLE


def test_empty_plan_is_reported_rather_than_looping() -> None:
    booking, _ = booking_for({})
    booking.court_date = date(2025, 7, 22)
    result = booking.sprint([], deadline=booking.waiter.now() + 1)

    assert result.booked is False
    assert "No candidate slots" in (result.detail or "")


# -- waiting for a date that opens on the clock ------------------------------
def test_await_date_keeps_reloading_until_the_date_appears() -> None:
    """A club's noon is its server's noon, and can run late."""
    from datetime import date as date_type

    opens_on_reload = {"n": 0}

    def day_title() -> str:
        # Not offered until the third page load.
        return "clicked" if opens_on_reload["n"] >= 3 else "missing"

    booking, driver = booking_for(
        {
            "toggle.click()": "opened",
            "days[i].getAttribute('title')": day_title,
            ".k-calendar .k-nav-next": "clicked",
            "CreateReservation\"]').length": 4,
        }
    )
    original_refresh = driver.refresh

    def counting_refresh() -> None:
        opens_on_reload["n"] += 1
        original_refresh()

    driver.refresh = counting_refresh  # type: ignore[method-assign]

    waited = booking.await_date(date_type(2025, 7, 22), timeout=10)

    assert waited >= 0
    assert opens_on_reload["n"] >= 3, "must reload until the date is offered"


def test_await_date_blames_the_configuration_when_the_date_is_too_far_out() -> None:
    """The failure that was mistaken for a late release.

    Asking for a date further ahead than the facility opens can never succeed,
    and reads identically to a slow club unless the message does the arithmetic.
    """
    from datetime import timedelta

    booking, _ = booking_for(
        {"toggle.click()": "opened", "days[i].getAttribute('title')": "missing",
         ".k-calendar .k-nav-next": "clicked"}
    )
    too_far = now_in(booking.settings.tz).date() + timedelta(
        days=booking.settings.days_in_advance + 2
    )

    with pytest.raises(BookingError) as caught:
        booking.await_date(too_far, timeout=0)

    message = str(caught.value)
    assert "days out but days_in_advance is" in message
    assert "not a late release" in message


def test_await_date_blames_the_release_when_the_date_is_the_right_distance_out() -> None:
    from datetime import timedelta

    booking, _ = booking_for(
        {"toggle.click()": "opened", "days[i].getAttribute('title')": "missing",
         ".k-calendar .k-nav-next": "clicked"}
    )
    on_schedule = now_in(booking.settings.tz).date() + timedelta(
        days=booking.settings.days_in_advance
    )

    with pytest.raises(BookingError) as caught:
        booking.await_date(on_schedule, timeout=0)

    message = str(caught.value)
    assert "matches days_in_advance" in message
    assert "release_grace_seconds" in message


def test_the_grace_window_comes_from_configuration() -> None:
    from pickletime.settings import BookingSettings

    assert BookingSettings().release_grace_seconds == 180
    assert BookingSettings(release_grace_seconds=300).release_grace_seconds == 300
    with pytest.raises(ValueError):
        BookingSettings(release_grace_seconds=901)
