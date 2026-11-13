"""Polling helpers used instead of fixed sleeps.

Two changes here decide whether the bot wins a slot race:

**No implicit wait.** The old driver set ``implicitly_wait(timeout)`` *and* used
explicit waits. Selenium warns against mixing them, and the cost was concrete:
every locator that missed blocked for the full implicit timeout inside
``find_element``, so a five-candidate fallback list could burn a minute before
finding the element that was there all along.

**Locators race, they don't queue.** :meth:`Waiter.find_first` evaluates all
candidates on each poll, so a list of ten fallbacks costs the same wall-clock as
one. The previous helper gave each candidate its own five-second budget.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from selenium.common.exceptions import (
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.remote.webelement import WebElement

from ..errors import ElementNotFoundError
from ..logging_setup import get_logger

Locator = tuple[str, str]
logger = get_logger()

_TRANSIENT = (StaleElementReferenceException, NoSuchElementException)


class Waiter:
    """Condition-polling wrapper around a WebDriver."""

    def __init__(
        self,
        driver: Any,
        *,
        default_timeout: float = 15.0,
        poll_interval: float = 0.1,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self.driver = driver
        self.default_timeout = default_timeout
        self.poll_interval = poll_interval
        self._clock = clock
        self._sleep = sleep

    # -- generic polling --------------------------------------------------
    def until(
        self,
        predicate: Callable[[], Any],
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> Any:
        """Poll ``predicate`` until it returns something truthy, or time out.

        Returns the truthy value, or ``None`` on timeout — callers decide
        whether a miss is fatal, so this never raises for an absent element.
        """
        limit = self.default_timeout if timeout is None else timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = self._clock() + limit
        while True:
            try:
                result = predicate()
                if result:
                    return result
            except _TRANSIENT:
                pass
            except (JavascriptException, WebDriverException) as exc:
                logger.debug(f"Transient error while polling: {type(exc).__name__}: {exc}")
            if self._clock() >= deadline:
                return None
            self._sleep(min(interval, max(deadline - self._clock(), 0.0)))

    # -- element lookup ---------------------------------------------------
    def match_now(self, locators: Sequence[Locator], *, visible: bool = False) -> WebElement | None:
        """Single non-blocking pass over ``locators``; ``None`` if nothing matches right now."""
        for by, value in locators:
            try:
                for element in self.driver.find_elements(by, value):
                    if not visible or element.is_displayed():
                        return element
            except _TRANSIENT:
                continue
            except WebDriverException:
                continue
        return None

    def find_first(
        self,
        locators: Sequence[Locator],
        *,
        timeout: float | None = None,
        visible: bool = False,
    ) -> WebElement | None:
        """First element matching any locator, or ``None`` if none appear in time."""
        return self.until(lambda: self.match_now(locators, visible=visible), timeout=timeout)

    def require_first(
        self,
        locators: Sequence[Locator],
        description: str,
        *,
        timeout: float | None = None,
        visible: bool = False,
    ) -> WebElement:
        """Like :meth:`find_first`, but raises when nothing matches."""
        element = self.find_first(locators, timeout=timeout, visible=visible)
        if element is None:
            raise ElementNotFoundError(
                f"Could not find {description} (tried {len(locators)} selectors)"
            )
        return element

    def gone(self, selector: str, *, timeout: float | None = None) -> bool:
        """True once no *visible* element matches ``selector``."""
        from selenium.webdriver.common.by import By

        result = self.until(
            lambda: self.match_now([(By.CSS_SELECTOR, selector)], visible=True) is None,
            timeout=timeout,
        )
        return bool(result)

    # -- interaction ------------------------------------------------------
    def click(self, element: WebElement, description: str = "element") -> bool:
        """Click via JS, then natively, then via ActionChains."""
        attempts: Iterable[tuple[str, Callable[[], None]]] = (
            ("javascript", lambda: self.driver.execute_script("arguments[0].click();", element)),
            ("native", element.click),
            (
                "actionchains",
                lambda: ActionChains(self.driver).move_to_element(element).click().perform(),
            ),
        )
        for name, action in attempts:
            try:
                action()
                logger.debug(f"Clicked {description} ({name})")
                return True
            except Exception as exc:  # noqa: BLE001 - deliberately try the next strategy
                logger.debug(f"{name} click failed for {description}: {type(exc).__name__}: {exc}")
        logger.warning(f"All click strategies failed for {description}")
        return False

    def js(self, script: str, *args: Any) -> Any:
        """Run JavaScript, returning ``None`` instead of raising on a JS error."""
        try:
            return self.driver.execute_script(script, *args)
        except JavascriptException as exc:
            logger.debug(f"JavaScript execution failed: {exc}")
            return None

    def sleep(self, seconds: float) -> None:
        """Deliberate pause. Prefer :meth:`until`; this exists for genuine backoff."""
        if seconds > 0:
            self._sleep(seconds)

    def now(self) -> float:
        return self._clock()
