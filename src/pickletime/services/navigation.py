"""Getting from the post-login landing page to the sport's slot grid.

The old flow clicked "Reservations" (sleep 1s), re-clicked it if the URL had not
changed (sleep 2s), then hunted for the sport link and clicked that (sleep 2s) —
about five seconds of unconditional waiting plus two selector hunts, every run.

The destination is a plain GET:
``/Online/Reservations/Bookings/{org}?sId={sport}``.  Navigating there directly
is both faster and less brittle.  The menu walk survives as a fallback for the
case where the site starts requiring it.
"""

from __future__ import annotations

import time
from typing import Any

from ..core import locators
from ..core.waits import Waiter
from ..errors import NavigationError
from ..logging_setup import get_logger
from ..settings import BookingSettings

logger = get_logger()

#: The bookings page loaded, but not the calendar this engine knows how to read.
_NEW_LAYOUT_HINT = (
    "The bookings page loaded but shows neither the Kendo scheduler grid nor the "
    "member portal's scheduler: CourtReserve has changed this facility's page "
    "again. Nothing was booked. Run `run.sh capture --open-slot` and share its "
    "summary.txt so the engine can be updated."
)


class NavigationService:
    """Opens the booking calendar for the configured sport."""

    def __init__(self, driver: Any, waiter: Waiter, settings: BookingSettings) -> None:
        self.driver = driver
        self.waiter = waiter
        self.settings = settings
        #: "legacy" (Kendo grid) or "portal" (the newer single-page scheduler),
        #: set once the page has rendered.
        self.layout: str | None = None

    def open_booking_grid(self) -> float:
        """Land on the sport's slot grid. Returns elapsed seconds."""
        started = time.monotonic()

        self.driver.get(self.settings.bookings_url)
        if self._grid_ready():
            elapsed = time.monotonic() - started
            logger.info(f"Booking grid ready in {elapsed:.2f}s (direct, {self.layout} layout)")
            return time.monotonic() - started

        landed_on_bookings = "/reservations/bookings/" in self._current_url().lower()
        logger.warning("Direct navigation did not render the grid; falling back to the menu")
        try:
            self._navigate_via_menu()
        except NavigationError as exc:
            if landed_on_bookings:
                raise NavigationError(_NEW_LAYOUT_HINT) from exc
            raise

        if not self._grid_ready():
            if landed_on_bookings:
                raise NavigationError(_NEW_LAYOUT_HINT)
            raise NavigationError(
                "Could not reach the booking grid for "
                f"sport {self.settings.sport_id} at {self.settings.bookings_url}"
            )

        elapsed = time.monotonic() - started
        logger.info(f"Booking grid ready in {elapsed:.2f}s (menu fallback, {self.layout} layout)")
        return elapsed

    # -- internals --------------------------------------------------------
    def _grid_ready(self, timeout: float | None = None) -> bool:
        """Either layout's scheduler is on screen; records which in ``layout``."""
        limit = self.settings.webdriver_timeout if timeout is None else timeout
        portal = [("css selector", locators.PORTAL_SCHEDULER)]

        def detect() -> str | None:
            if self.waiter.match_now(locators.SCHEDULER_GRID) is not None:
                return "legacy"
            if self.waiter.match_now(portal) is not None:
                return "portal"
            return None

        self.layout = self.waiter.until(detect, timeout=limit)
        return self.layout is not None

    def _navigate_via_menu(self) -> None:
        reservations = self.waiter.find_first(locators.RESERVATIONS_LINK, timeout=5, visible=True)
        if reservations is not None:
            self.waiter.click(reservations, "reservations menu")
            self.waiter.until(lambda: self._on_reservations_url(), timeout=5)

        sport = self.waiter.find_first(
            [
                ("xpath", f"//a[contains(@href, 'sId={self.settings.sport_id}')]"),
                ("xpath", f"//a[contains(normalize-space(), '{self.settings.sport_name}')]"),
            ],
            timeout=5,
            visible=True,
        )
        if sport is None:
            raise NavigationError(
                f"Could not find a menu entry for {self.settings.sport_name} "
                f"(sId={self.settings.sport_id})"
            )
        self.waiter.click(sport, f"{self.settings.sport_name} menu entry")

    def _current_url(self) -> str:
        try:
            return str(self.driver.current_url or "")
        except Exception:  # noqa: BLE001 - treat a dead session as "nowhere yet"
            return ""

    def _on_reservations_url(self) -> bool:
        url = self._current_url().lower()
        return "reservations" in url or "bookings" in url
