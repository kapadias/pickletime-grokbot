"""Shared fixtures."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pickletime.logging_setup import configure_logging
from pickletime.settings import BookingSettings, Credentials, RuntimeSettings, Settings

configure_logging(level="WARNING", json_output=False)

_CHROME_CANDIDATES = (
    os.environ.get("PICKLETIME_TEST_CHROME"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

_DRIVER_CANDIDATES = (
    os.environ.get("PICKLETIME_TEST_CHROMEDRIVER"),
    "/usr/local/bin/chromedriver",
    "/usr/bin/chromedriver",
)


def _first_existing(candidates: tuple[str | None, ...]) -> str | None:
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    return None


def _major_version(executable: str) -> str | None:
    """The major version `exe --version` reports, or None if it will not say."""
    try:
        output = subprocess.run(  # noqa: S603 - fixed argv, path from our own list
            [executable, "--version"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except OSError:
        return None
    match = re.search(r"(\d+)\.\d+\.\d+", output)
    return match.group(1) if match else None


def _matched_pair(version: str) -> tuple[str, str] | None:
    """Ask Selenium Manager for a Chrome and chromedriver of the same major.

    Downloads and caches them on first use, so this needs the network once.
    """
    try:
        from selenium.webdriver.common.selenium_manager import (  # noqa: PLC0415
            SeleniumManager,
        )

        result = SeleniumManager().binary_paths(
            ["--browser", "chrome", "--browser-version", version]
        )
    except Exception:  # noqa: BLE001 - offline, or Selenium changed the API
        return None
    browser, driver = result.get("browser_path"), result.get("driver_path")
    if browser and driver and Path(browser).is_file() and Path(driver).is_file():
        return browser, driver
    return None


@pytest.fixture(scope="session")
def chrome_paths() -> tuple[str | None, str | None]:
    """(chrome binary, chromedriver) — a version-matched pair, or a skip.

    Selenium Manager takes a chromedriver already on PATH in preference to
    downloading one, so a stale driver from an unrelated toolchain turns every
    end-to-end test into a "session not created" failure that reads as a
    regression in this project. Worse, it does it silently enough that the
    whole e2e suite can sit red — or skipped — for a whole session, which is
    exactly when its coverage is most wanted. So on a mismatch, fetch a
    matching pair rather than giving up.
    """
    binary = _first_existing(_CHROME_CANDIDATES)
    if binary is None:
        pytest.skip("No Chrome/Chromium binary available for end-to-end tests")

    driver = _first_existing(_DRIVER_CANDIDATES)
    on_path = driver or shutil.which("chromedriver")
    if not on_path:
        return binary, driver

    browser_major, driver_major = _major_version(binary), _major_version(on_path)
    if not (browser_major and driver_major) or browser_major == driver_major:
        return binary, driver

    if (pair := _matched_pair(browser_major)) is not None:
        return pair

    pytest.skip(
        f"chromedriver {driver_major} at {on_path} cannot drive Chrome "
        f"{browser_major} at {binary}, and no matching pair could be fetched. "
        "Remove it from PATH, or point PICKLETIME_TEST_CHROME / "
        "PICKLETIME_TEST_CHROMEDRIVER at a matching pair."
    )


@pytest.fixture
def credentials() -> Credentials:
    return Credentials(username="player@example.com", password="correct-horse")


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        log_level="WARNING",
        log_json=False,
        ledger_path=str(tmp_path / "ledger.json"),
    )


@pytest.fixture
def make_settings(credentials: Credentials, runtime: RuntimeSettings):
    """Factory for a fully-formed Settings with sensible test defaults."""

    def factory(**overrides: object) -> Settings:
        defaults: dict[str, object] = {
            "search_start_time": "10:00 AM",
            "search_end_time": "12:00 PM",
            "preferred_durations": [60, 30],
            "booking_priority": "duration",
            "priority_time_slots": ["11:30 AM"],
            "headless": True,
            "webdriver_timeout": 8,
            "page_load_timeout": 20,
            "poll_interval_ms": 50,
            "sprint_seconds": 6,
            "max_retries": 0,
            "retry_interval": 0,
        }
        defaults.update(overrides)
        return Settings(
            booking=BookingSettings(**defaults),  # type: ignore[arg-type]
            credentials=credentials,
            runtime=runtime,
        )

    return factory


@pytest.fixture
def captured_logs():
    """Collect loguru output. pytest's caplog only sees stdlib logging."""
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message), level="DEBUG")

    class Captured:
        @property
        def text(self) -> str:
            return "".join(records)

    try:
        yield Captured()
    finally:
        logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Never let a developer's real .env leak into a test run."""
    monkeypatch.setenv("PICKLETIME_SKIP_DOTENV", "1")
