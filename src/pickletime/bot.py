"""Orchestrates a booking run: warm up, arm, sprint.

The timing strategy is the substance of this module, so it is worth stating
plainly what changed.

The previous flow was: scheduler fires at the release minute → ``time.sleep(30)``
→ start Chrome → log in (with ~8s of unconditional ``sleep`` calls) → click
through two menus (another ~5s of sleeps) → open the datepicker → *then* start
trying slots. Roughly a minute of dead time landing entirely **after** the moment
the courts became available, against a site where the good slots go in seconds.

The flow now is:

``warm up`` — start Chrome, log in, open the grid, and select the target date
**before** the release moment, so all of the slow work is already done;

``arm`` — wait on the wall clock until ``release_time`` in the facility's
timezone, accurate to hundredths of a second;

Warming up early has one consequence that is easy to miss, and it cost a real
booking. Some facilities do not merely *lock* the Nth day out until the release
hour — they do not offer that date at all until then. So the "select the target
date" step above can legitimately find nothing, and treating that as an error
kills the run minutes before the date it is waiting for, turning the entire
point of the lead time into the cause of failure. When a release is still
pending, an absent date is expected: the run arms anyway and claims the date the
moment it appears (see :meth:`BookingService.await_date`). Only when there is no
release left to wait for is a missing date a real misconfiguration.

``sprint`` — refresh the slot data source and work the candidate plan in a tight
loop until something books or the window closes, re-walking the plan because a
slot missing on the first pass is usually there a moment later.

Set ``release_time`` and start the run a couple of minutes earlier; the
bot handles the rest. Leave ``release_time`` unset and it behaves like the old
one-shot bot, minus the dead time.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from typing import Any

from .core.browser import BrowserManager, log_renderer_crash_hint
from .errors import BookingError, RetryableError, TerminalError
from .ledger import BookingLedger, NullLedger, build_ledger
from .logging_setup import get_logger
from .notify import Notifier, build_notifier
from .services.auth import AuthService
from .services.booking import BookingResult, BookingService
from .services.navigation import NavigationService
from .settings import Settings
from .timeutil import (
    format_long_date,
    format_us_date,
    next_occurrence_of,
    now_in,
    sleep_until,
)

logger = get_logger()

__all__ = ["CourtBookingBot", "RunReport"]


@dataclass
class RunReport:
    """Everything a caller (CLI exit code, HTTP response, alert) needs."""

    run_id: str
    booked: bool
    court_date: str
    dry_run: bool = False
    slot_time: str | None = None
    duration: int | None = None
    confirmation: str = "none"
    detail: str | None = None
    attempts: int = 0
    unavailable: int = 0
    rejected: int = 0
    retries_used: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "booked": self.booked,
            "date": self.court_date,
            "dry_run": self.dry_run,
            "time": self.slot_time,
            "duration": self.duration,
            "confirmation": self.confirmation,
            "detail": self.detail,
            "attempts": self.attempts,
            "unavailable": self.unavailable,
            "rejected": self.rejected,
            "retries_used": self.retries_used,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "timings_seconds": {k: round(v, 3) for k, v in self.timings.items()},
        }

    @property
    def notification_details(self) -> dict[str, Any]:
        return {
            "date": self.court_date,
            "time": self.slot_time,
            "duration": self.duration,
            "confirmation": self.confirmation,
            "run_id": self.run_id,
        }


def booking_service_for(
    layout: str | None, driver: Any, waiter: Any, settings: Any
) -> BookingService:
    """The booking service that speaks the layout the bookings page rendered."""
    if layout == "portal":
        from .services.portal import PortalBookingService  # noqa: PLC0415

        return PortalBookingService(driver, waiter, settings)
    return BookingService(driver, waiter, settings)


class CourtBookingBot:
    """Runs one booking attempt end to end."""

    def __init__(
        self,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
        ledger: BookingLedger | NullLedger | None = None,
        browser_factory: Any | None = None,
        run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.booking_settings = settings.booking
        self.notifier = notifier or build_notifier(settings.booking)
        self.ledger = ledger if ledger is not None else build_ledger(
            settings.runtime.ledger_path, settings.runtime.ledger_ttl_hours
        )
        self._browser_factory = browser_factory or (lambda: BrowserManager(settings.booking))
        self.run_id = run_id or uuid.uuid4().hex[:12]

    # -- public API -------------------------------------------------------
    def run(self) -> RunReport:
        target = self.settings.booking.target_date()
        # Which times to want depends on the day, so the effective settings are
        # only knowable once the court date is. Everything downstream reads
        # self.booking_settings, so resolve it before anything uses the window.
        self.booking_settings = self.settings.booking.for_date(target)
        settings = self.booking_settings
        date_label = format_us_date(target)
        log = logger.bind(run_id=self.run_id, court_date=date_label)

        log.info(
            f"Run {self.run_id}: booking {date_label} ({target:%A}) between "
            f"{settings.search_start_time:%I:%M %p} and {settings.search_end_time:%I:%M %p} "
            f"(priority={settings.booking_priority}, durations={settings.preferred_durations}, "
            f"dry_run={settings.dry_run})"
        )

        guard = self._skip_outside_rotation(target, date_label) or self._check_ledger(date_label)
        if guard is not None:
            return guard

        total_started = time.monotonic()
        last_error: Exception | None = None
        # Owned here, not inside _execute, so a crash mid-run still tells us which
        # phase it reached. Without this a Chrome death reports only a total.
        timings: dict[str, float] = {}

        for attempt in range(settings.max_retries + 1):
            try:
                timings.clear()
                result = self._execute(target, timings)
                timings["total"] = time.monotonic() - total_started
                report = self._report_from(result, timings, retries_used=attempt)
                self._finish(report)
                return report

            except TerminalError as exc:
                log.error(f"Terminal failure: {type(exc).__name__}: {exc}")
                report = RunReport(
                    run_id=self.run_id,
                    booked=False,
                    court_date=date_label,
                    dry_run=settings.dry_run,
                    error=f"{type(exc).__name__}: {exc}",
                    retries_used=attempt,
                    timings={**timings, "total": time.monotonic() - total_started},
                )
                self.notifier.error(str(exc), report.notification_details)
                return report

            except (RetryableError, Exception) as exc:  # noqa: BLE001 - retried below
                last_error = exc
                remaining = settings.max_retries - attempt
                reached = " ".join(f"{k}={v:.2f}s" for k, v in timings.items()) or "nothing"
                log.warning(
                    f"Attempt {attempt + 1}/{settings.max_retries + 1} failed after "
                    f"[{reached}]: {type(exc).__name__}: {str(exc).splitlines()[0]}"
                )
                log_renderer_crash_hint(exc)
                if remaining <= 0:
                    break
                backoff = settings.retry_interval * (attempt + 1)
                log.info(f"Retrying in {backoff}s ({remaining} attempt(s) left)")
                time.sleep(backoff)

        message = f"{type(last_error).__name__}: {last_error}"
        log.error(f"All attempts exhausted: {message}")
        report = RunReport(
            run_id=self.run_id,
            booked=False,
            court_date=date_label,
            dry_run=settings.dry_run,
            error=message,
            retries_used=settings.max_retries,
            timings={**timings, "total": time.monotonic() - total_started},
        )
        self.notifier.error(message, report.notification_details)
        return report

    def diagnose(self) -> dict[str, Any]:
        """Log in, open the grid, and dump what the site actually renders."""
        target = self.settings.booking.target_date()
        self.booking_settings = self.settings.booking.for_date(target)
        settings = self.booking_settings

        with self._browser_factory() as (driver, waiter):
            auth = AuthService(driver, waiter, settings, self.settings.credentials)
            navigation = NavigationService(driver, waiter, settings)

            auth.login()
            navigation.open_booking_grid()
            layout = getattr(navigation, "layout", None)
            booking = booking_service_for(layout, driver, waiter, settings)
            booking.select_date(target)

            report = booking.diagnose(booking.build_plan(target))

        report["config"] = {
            "organization_id": settings.organization_id,
            "sport_id": settings.sport_id,
            "court_type": settings.court_type,
            "search_window": [
                settings.search_start_time.strftime("%I:%M %p"),
                settings.search_end_time.strftime("%I:%M %p"),
            ],
            "preferred_durations": settings.preferred_durations,
            "duration_labels_expected": [
                settings.label_for_duration(d) for d in settings.preferred_durations
            ],
            "slot_length_minutes": settings.slot_length_minutes,
        }
        return report

    # -- phases -----------------------------------------------------------
    def _execute(self, target: date_type, timings: dict[str, float]) -> BookingResult:
        """Run one full attempt, recording phase timings into ``timings`` as it goes.

        ``timings`` belongs to the caller so that a crash part-way through still
        shows how far the run got.
        """
        settings = self.booking_settings
        started = time.monotonic()

        with self._browser_factory() as (driver, waiter):
            timings["chrome"] = time.monotonic() - started

            auth = AuthService(driver, waiter, settings, self.settings.credentials)
            navigation = NavigationService(driver, waiter, settings)

            timings["login"] = auth.login()
            timings["navigate"] = navigation.open_booking_grid()
            layout = getattr(navigation, "layout", None)
            booking = booking_service_for(layout, driver, waiter, settings)

            # The target date may not exist in the picker yet: a facility that
            # releases the Nth day out on the clock adds it at the release
            # moment, and warm-up runs deliberately before that. Treating its
            # absence as an error killed the run three minutes before the date
            # it was waiting for, which is the exact opposite of the point.
            selected = booking.try_select_date(target)
            if selected is None and self._seconds_until_release() <= 0:
                raise BookingError(
                    f"Date {format_long_date(target)} is not selectable in the picker, "
                    "and there is no upcoming release to wait for. Check "
                    "days_in_advance against how far ahead the facility opens."
                )
            timings["select_date"] = selected or 0.0
            if selected is None:
                logger.info(
                    f"{format_us_date(target)} is not open yet — expected this far "
                    "ahead of release; will take it the moment it appears"
                )

            plan = booking.build_plan(target)
            logger.info(
                f"Plan: {len(plan)} candidate(s) across "
                f"{len(settings.preferred_durations)} duration(s)"
            )

            timings["armed_wait"] = self._arm()
            if selected is None:
                timings["select_date"] = booking.await_date(target)
            else:
                booking.refresh_grid()

            sprint_started = time.monotonic()
            deadline = waiter.now() + max(settings.sprint_seconds, 1)
            result = booking.sprint(plan, deadline=deadline)
            timings["sprint"] = time.monotonic() - sprint_started

        result.timings = dict(timings)
        return result

    def _release_moment(self) -> datetime | None:
        """When the next release falls, or None if there is nothing to wait for."""
        settings = self.booking_settings
        if settings.release_time is None:
            return None
        tz = settings.tz
        # One clock reading for both decisions: reading it twice can straddle the
        # release second and arm for tomorrow.
        reference = now_in(tz)
        fire_at = next_occurrence_of(
            settings.release_time, tz, now=reference, tolerance_seconds=120
        )
        if (fire_at - reference).total_seconds() > settings.max_arm_wait_seconds:
            return None
        return fire_at

    def _seconds_until_release(self) -> float:
        """How long until release; 0 when it has passed or is not worth holding for."""
        fire_at = self._release_moment()
        if fire_at is None:
            return 0.0
        return max((fire_at - now_in(self.booking_settings.tz)).total_seconds(), 0.0)

    def _arm(self) -> float:
        """Hold until the release moment."""
        settings = self.booking_settings
        if settings.release_time is None:
            logger.info("No release_time configured — sprinting immediately")
            return 0.0

        tz = settings.tz
        reference = now_in(tz)
        fire_at = next_occurrence_of(
            settings.release_time, tz, now=reference, tolerance_seconds=120
        )
        wait_seconds = (fire_at - reference).total_seconds()

        if wait_seconds <= 0:
            logger.info(
                f"Release time {fire_at:%H:%M:%S %Z} already passed — sprinting immediately"
            )
            return 0.0

        if wait_seconds > settings.max_arm_wait_seconds:
            logger.warning(
                f"Next release is {wait_seconds / 60:.1f} min away, over the "
                f"{settings.max_arm_wait_seconds}s arming cap — sprinting now instead of "
                "holding the request open. Schedule the trigger closer to release_time."
            )
            return 0.0

        logger.info(
            f"Warm-up done with {wait_seconds:.1f}s to spare; "
            f"holding until {fire_at:%Y-%m-%d %H:%M:%S %Z}"
        )
        waited = sleep_until(fire_at, max_wait_seconds=settings.max_arm_wait_seconds + 5)
        overshoot = (now_in(tz) - fire_at).total_seconds()
        logger.info(f"Release reached (+{overshoot * 1000:.0f}ms)")
        return waited

    # -- helpers ----------------------------------------------------------
    def _skip_outside_rotation(self, target: date_type, date_label: str) -> RunReport | None:
        """Bail out before starting Chrome if this is not a day we play.

        The scheduler fires daily because a 14-day rotation cannot be expressed
        in cron; most firings are expected to land here and cost nothing.
        """
        rotation = self.booking_settings.rotation
        if rotation is None or rotation.is_play_date(target):
            return None

        reason = (
            f"{date_label} ({target:%A}) is cycle day "
            f"{rotation.offset_of(target)}, which is not in "
            f"{rotation.play_on_offsets} — not a play day"
        )
        logger.info(f"Skipping: {reason}")
        return RunReport(
            run_id=self.run_id,
            booked=False,
            court_date=date_label,
            dry_run=self.booking_settings.dry_run,
            skipped_reason="outside the rotation",
            detail=reason,
        )

    def _check_ledger(self, date_label: str) -> RunReport | None:
        settings = self.booking_settings
        key = BookingLedger.key_for(settings.organization_id, settings.sport_id, date_label)
        existing = self.ledger.lookup(key)
        if existing is None:
            return None

        logger.warning(
            f"{date_label} is already recorded as booked at "
            f"{existing.recorded_at.isoformat()} — skipping to avoid a double booking. "
            "Clear the ledger file to force a re-run."
        )
        payload = existing.payload
        return RunReport(
            run_id=self.run_id,
            booked=True,
            court_date=date_label,
            dry_run=settings.dry_run,
            slot_time=payload.get("time"),
            duration=payload.get("duration"),
            confirmation=str(payload.get("confirmation", "ledger")),
            skipped_reason="already booked (ledger)",
            detail="Skipped: a booking for this date was already recorded",
        )

    def _report_from(
        self, result: BookingResult, timings: dict[str, float], *, retries_used: int
    ) -> RunReport:
        return RunReport(
            run_id=self.run_id,
            booked=result.booked,
            court_date=result.court_date,
            dry_run=result.dry_run,
            slot_time=result.slot_time,
            duration=result.duration,
            confirmation=result.confirmation,
            detail=result.detail,
            attempts=result.attempts,
            unavailable=result.unavailable,
            rejected=result.rejected,
            retries_used=retries_used,
            timings=timings,
        )

    def _finish(self, report: RunReport) -> None:
        settings = self.booking_settings
        summary = (
            f"attempts={report.attempts} unavailable={report.unavailable} "
            f"rejected={report.rejected} "
            + " ".join(f"{k}={v:.2f}s" for k, v in sorted(report.timings.items()))
        )

        if report.booked and not report.dry_run:
            logger.success(
                f"Booked {report.court_date} at {report.slot_time} for "
                f"{report.duration}min ({report.confirmation}) | {summary}"
            )
            key = BookingLedger.key_for(
                settings.organization_id, settings.sport_id, report.court_date
            )
            self.ledger.record(
                key,
                {
                    "time": report.slot_time,
                    "duration": report.duration,
                    "confirmation": report.confirmation,
                    "run_id": self.run_id,
                    "booked_at": datetime.now(settings.tz).isoformat(),
                },
            )
            self.notifier.success(report.notification_details)
            return

        if report.dry_run:
            logger.warning(f"Dry run finished without booking | {summary}")
            return

        if report.confirmation == "unverified":
            # Record it anyway. The entry is what stops the retry job — or
            # tomorrow's run — from booking a second court on an evening that
            # may already have one. It is deliberately not a success: no
            # "booked" alert goes out, because the court may not exist.
            reason = (
                f"{report.court_date} at {report.slot_time}: the site neither confirmed "
                f"nor refused the booking, so it may or may not exist. Check your "
                f"reservations. Nothing further will be attempted for this date. "
                f"{report.detail or ''}"
            ).strip()
            logger.error(f"Unverified: {reason} | {summary}")
            key = BookingLedger.key_for(
                settings.organization_id, settings.sport_id, report.court_date
            )
            self.ledger.record(
                key,
                {
                    "time": report.slot_time,
                    "duration": report.duration,
                    "confirmation": "unverified",
                    "run_id": self.run_id,
                    "booked_at": datetime.now(settings.tz).isoformat(),
                },
            )
            self.notifier.error(reason, report.notification_details)
            return

        reason = report.detail or "No available time slots in the search window"
        logger.warning(f"Not booked: {reason} | {summary}")
        self.notifier.failure(reason, report.notification_details)
