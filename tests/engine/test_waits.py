"""Wait helpers — the timing behaviour that decides whether a slot is caught."""

from __future__ import annotations

import pytest
from fakes import FakeClock, FakeDriver, FakeElement, counter
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from pickletime.core.waits import Waiter
from pickletime.errors import ElementNotFoundError


def build(driver: FakeDriver, timeout: float = 5.0) -> tuple[Waiter, FakeClock]:
    clock = FakeClock()
    waiter = Waiter(
        driver,
        default_timeout=timeout,
        poll_interval=0.1,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return waiter, clock


def test_locators_race_rather_than_queue() -> None:
    """N fallback selectors cost one timeout, not N timeouts.

    The old helper gave every candidate its own ``WebDriverWait(5)``, so a list
    of seven email-field selectors could burn 35 seconds before finding the one
    that matched.
    """
    target = FakeElement()
    driver = FakeDriver({(By.ID, "found"): [target]})
    waiter, clock = build(driver, timeout=5.0)

    locators = [(By.ID, f"missing-{i}") for i in range(6)] + [(By.ID, "found")]
    assert waiter.find_first(locators) is target
    assert clock.value == 0.0  # matched on the first poll, no waiting at all


def test_find_first_gives_up_after_one_shared_timeout() -> None:
    driver = FakeDriver({})
    waiter, clock = build(driver, timeout=2.0)

    assert waiter.find_first([(By.ID, f"nope-{i}") for i in range(10)]) is None
    assert clock.value == pytest.approx(2.0, abs=0.2)


def test_find_first_waits_for_a_late_element() -> None:
    driver = FakeDriver({})
    waiter, clock = build(driver, timeout=5.0)
    late = FakeElement()

    calls = {"n": 0}
    original = driver.find_elements

    def delayed(by: str, value: str) -> list[FakeElement]:
        calls["n"] += 1
        return [late] if calls["n"] > 3 else original(by, value)

    driver.find_elements = delayed  # type: ignore[method-assign]
    assert waiter.find_first([(By.ID, "slow")]) is late
    assert clock.value > 0


def test_visible_filter_skips_hidden_matches() -> None:
    hidden = FakeElement(displayed=False)
    shown = FakeElement(displayed=True)
    driver = FakeDriver({(By.ID, "x"): [hidden, shown]})
    waiter, _ = build(driver)

    assert waiter.match_now([(By.ID, "x")], visible=False) is hidden
    assert waiter.match_now([(By.ID, "x")], visible=True) is shown


def test_require_first_raises_with_a_useful_message() -> None:
    waiter, _ = build(FakeDriver({}), timeout=0.2)
    with pytest.raises(ElementNotFoundError, match="password field.*3 selectors"):
        waiter.require_first([(By.ID, "a"), (By.ID, "b"), (By.ID, "c")], "password field")


def test_stale_elements_do_not_abort_a_poll() -> None:
    driver = FakeDriver({})
    waiter, _ = build(driver, timeout=1.0)
    found = FakeElement()
    answers = counter([StaleElementReferenceException(), [found]])

    def flaky(by: str, value: str):
        answer = answers()
        if isinstance(answer, Exception):
            raise answer
        return answer

    driver.find_elements = flaky  # type: ignore[method-assign]
    assert waiter.find_first([(By.ID, "x")]) is found


def test_webdriver_errors_during_polling_are_survivable() -> None:
    driver = FakeDriver({})
    waiter, _ = build(driver, timeout=0.5)

    def boom() -> None:
        raise WebDriverException("connection reset")

    assert waiter.until(boom) is None  # times out rather than propagating


def test_click_falls_through_strategies() -> None:
    element = FakeElement()
    driver = FakeDriver({})

    def failing_script(script: str, *args: object) -> None:
        raise JavascriptException("no")

    driver.execute_script = failing_script  # type: ignore[method-assign]
    waiter, _ = build(driver)

    assert waiter.click(element, "thing") is True
    assert element.clicks == 1  # fell back to the native click


def test_js_swallows_script_errors() -> None:
    driver = FakeDriver({})

    def failing_script(script: str, *args: object) -> None:
        raise JavascriptException("boom")

    driver.execute_script = failing_script  # type: ignore[method-assign]
    waiter, _ = build(driver)
    assert waiter.js("return 1;") is None


def test_gone_reports_when_an_element_stops_being_visible() -> None:
    element = FakeElement(displayed=True)
    driver = FakeDriver({(By.CSS_SELECTOR, ".modal"): [element]})
    waiter, _ = build(driver, timeout=1.0)

    assert waiter.gone(".modal", timeout=0.3) is False
    element.displayed = False
    assert waiter.gone(".modal", timeout=0.3) is True
