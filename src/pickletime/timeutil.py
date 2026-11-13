"""Timezone-aware clock and calendar helpers.

Every date decision in this bot is made in the *facility's* timezone, never the
container's.  Cloud Run runs in UTC, so computing "eight days from now" with a
naive ``datetime.now()`` silently books the wrong day for any run that happens
after 17:00 Pacific.  All date math goes through :func:`resolve_court_date`.

The string formatters here also avoid the glibc-only ``%-d``/``%-m`` strftime
extensions, which raise ``ValueError`` on Windows and musl-based images.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError

__all__ = [
    "get_timezone",
    "now_in",
    "parse_clock_time",
    "format_clock_time",
    "format_us_date",
    "format_long_date",
    "parse_us_date",
    "resolve_court_date",
    "next_occurrence_of",
    "sleep_until",
]

#: Injected in tests so the sprint loop does not really sleep.
SleepFn = Callable[[float], None]
NowFn = Callable[[], datetime]


def get_timezone(name: str) -> ZoneInfo:
    """Return the tzdata zone for ``name``, with a helpful error if missing."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env dependent
        raise ConfigurationError(
            f"Unknown timezone {name!r}. Install the 'tzdata' package or use a "
            "valid IANA name such as 'America/Los_Angeles'."
        ) from exc


def now_in(tz: ZoneInfo) -> datetime:
    """Current wall-clock time in ``tz`` (always timezone-aware)."""
    return datetime.now(tz)


def parse_clock_time(value: str) -> time:
    """Parse a ``"7:30 PM"`` style clock string.

    Accepts a leading zero (``"07:30 PM"``) and any capitalisation of the
    meridiem, because both show up in hand-edited YAML.
    """
    text = value.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Time must look like '7:30 PM', got {value!r}")


def format_clock_time(value: time) -> str:
    """Render a time the way CourtReserve renders it: ``"7:30 PM"``.

    No leading zero on the hour — the site's ``data-href`` attributes are matched
    verbatim, so ``"07:30 PM"`` would never find a slot.
    """
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {meridiem}"


def format_us_date(value: date) -> str:
    """Render ``"7/22/2025"`` — no zero padding, matching the site's URLs."""
    return f"{value.month}/{value.day}/{value.year}"


def format_long_date(value: date) -> str:
    """Render ``"Tuesday, July 22, 2025"`` — the datepicker's ``title`` format."""
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def parse_us_date(value: str) -> date:
    """Parse ``"07/22/2025"`` (the documented ``--date`` / ``court_date`` format)."""
    return datetime.strptime(value.strip(), "%m/%d/%Y").date()


def resolve_court_date(
    days_in_advance: int,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> date:
    """The date to book: ``days_in_advance`` days after *today in ``tz``*."""
    reference = now.astimezone(tz) if now is not None else now_in(tz)
    return reference.date() + timedelta(days=days_in_advance)


def next_occurrence_of(
    target: time,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
    tolerance_seconds: float = 0.0,
) -> datetime:
    """The next moment the wall clock in ``tz`` reads ``target``.

    If ``target`` has already passed today by no more than ``tolerance_seconds``,
    today's occurrence is returned so that a scheduler firing a hair late still
    sprints immediately instead of arming for tomorrow.
    """
    reference = (now.astimezone(tz) if now is not None else now_in(tz)).replace(microsecond=0)
    candidate = datetime.combine(reference.date(), target, tzinfo=tz)
    if candidate < reference - timedelta(seconds=tolerance_seconds):
        candidate = datetime.combine(reference.date() + timedelta(days=1), target, tzinfo=tz)
    return candidate


def sleep_until(
    target: datetime,
    *,
    now_fn: NowFn | None = None,
    sleep_fn: SleepFn = _time.sleep,
    max_wait_seconds: float = 3600.0,
    fine_window_seconds: float = 0.75,
    coarse_chunk_seconds: float = 5.0,
) -> float:
    """Block until ``target``, then return how long we waited.

    Two-phase so the wake-up is accurate without spinning the CPU for minutes:
    coarse chunks until we are within ``fine_window_seconds``, then short naps.
    Chunking also means a clock adjustment mid-wait is picked up on the next
    iteration rather than being baked into one long sleep.

    ``max_wait_seconds`` is a guard rail: a misconfigured release time should
    never park a Cloud Run request for hours.
    """
    tz = target.tzinfo
    if tz is None:
        raise ValueError("sleep_until requires a timezone-aware target")
    clock: NowFn = now_fn or (lambda: datetime.now(tz))

    started = clock()
    deadline_guard = started + timedelta(seconds=max_wait_seconds)
    effective_target = min(target, deadline_guard)

    while True:
        remaining = (effective_target - clock()).total_seconds()
        if remaining <= 0:
            break
        if remaining > fine_window_seconds:
            sleep_fn(min(remaining - fine_window_seconds, coarse_chunk_seconds))
        else:
            sleep_fn(min(remaining, 0.01))

    return max((clock() - started).total_seconds(), 0.0)
