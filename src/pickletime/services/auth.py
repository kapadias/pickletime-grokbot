"""Login against the CourtReserve account portal."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from selenium.webdriver.common.keys import Keys

from ..core import locators
from ..core.waits import Waiter
from ..errors import LoginError
from ..logging_setup import get_logger
from ..settings import BookingSettings, Credentials

logger = get_logger()


class AuthService:
    """Signs the bot in and confirms the session is real."""

    def __init__(
        self,
        driver: Any,
        waiter: Waiter,
        settings: BookingSettings,
        credentials: Credentials,
    ) -> None:
        self.driver = driver
        self.waiter = waiter
        self.settings = settings
        self.credentials = credentials

    def login(self) -> float:
        """Log in. Returns elapsed seconds. Raises :class:`LoginError` on failure."""
        started = time.monotonic()
        url = self.settings.login_url
        logger.info(f"Logging in as {self.credentials.masked_username}")
        self.driver.get(url)

        email = self.waiter.find_first(
            locators.EMAIL_FIELD, timeout=self.settings.webdriver_timeout
        )
        if email is None:
            self.screenshot("login-no-email-field")
            raise LoginError(
                "Login page did not render an email field — the site layout may have "
                f"changed, or {url} is unreachable"
            )

        password = self.waiter.require_first(locators.PASSWORD_FIELD, "password field", timeout=5)

        self._type(email, self.credentials.username)
        self._type(password, self.credentials.password)
        self._submit(password)

        self._await_outcome()
        elapsed = time.monotonic() - started
        logger.info(f"Login complete in {elapsed:.2f}s")
        return elapsed

    # -- internals --------------------------------------------------------
    def _type(self, element: Any, value: str) -> None:
        try:
            element.clear()
        except Exception as exc:  # noqa: BLE001 - some inputs reject clear()
            logger.debug(f"Could not clear field: {type(exc).__name__}: {exc}")
        element.send_keys(value)

    def _submit(self, password_field: Any) -> None:
        button = self.waiter.find_first(locators.SUBMIT_BUTTON, timeout=3, visible=True)
        if button is not None and self.waiter.click(button, "login submit"):
            return
        logger.debug("No usable submit button; submitting with Enter")
        password_field.send_keys(Keys.RETURN)

    def _await_outcome(self) -> None:
        """Wait for one of: authenticated shell, an error message, or a timeout."""
        timeout = self.settings.webdriver_timeout

        def outcome() -> str | None:
            if self.waiter.match_now(locators.AUTHENTICATED_MARKER, visible=False) is not None:
                return "authenticated"
            if "/account/login" not in self._current_url().lower():
                return "navigated"
            error = self.waiter.match_now(locators.LOGIN_ERROR, visible=True)
            if error is not None and (error.text or "").strip():
                return f"error:{error.text.strip()[:200]}"
            return None

        result = self.waiter.until(outcome, timeout=timeout)

        if result is None:
            self.screenshot("login-timeout")
            raise LoginError(
                f"Login did not complete within {timeout}s — still at {self._current_url()}"
            )
        if result.startswith("error:"):
            self.screenshot("login-rejected")
            raise LoginError(f"Login rejected by the site: {result[len('error:'):]}")

    def _current_url(self) -> str:
        try:
            return str(self.driver.current_url or "")
        except Exception:  # noqa: BLE001 - a dead session should surface as a login failure
            return ""

    def screenshot(self, name: str) -> str | None:
        """Save a debug screenshot when ``screenshot_dir`` is configured."""
        directory = self.settings.screenshot_dir
        if not directory:
            return None
        try:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            path = str(target / f"{name}-{int(time.time())}.png")
            self.driver.save_screenshot(path)
            logger.info(f"Screenshot saved: {path}")
            return path
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the run
            logger.warning(f"Could not save screenshot {name}: {type(exc).__name__}: {exc}")
            return None
