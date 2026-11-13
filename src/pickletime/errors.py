"""Exception hierarchy for the PickleTime booking bot.

The split that matters operationally is :class:`RetryableError` vs
:class:`TerminalError`.  The orchestrator only restarts the browser and replays
the whole flow for retryable faults; a terminal outcome (bad credentials, for
instance) is reported immediately instead of burning the booking window on
attempts that cannot succeed.
"""

from __future__ import annotations

__all__ = [
    "PickleTimeError",
    "ConfigurationError",
    "RetryableError",
    "TerminalError",
    "BrowserStartError",
    "LoginError",
    "NavigationError",
    "ElementNotFoundError",
    "BookingError",
]


class PickleTimeError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(PickleTimeError):
    """Configuration is invalid, missing, or internally inconsistent."""


class RetryableError(PickleTimeError):
    """A transient fault; replaying the flow may succeed."""


class TerminalError(PickleTimeError):
    """A definitive outcome; replaying the flow cannot change it.

    Note that "no slot was available" is deliberately *not* modelled here. It is
    an ordinary result, not a fault, and it travels back as a ``BookingResult``
    with ``booked=False``, so the runner reports "NOT BOOKED" rather than a
    failure, and nothing replays the run.
    """


class BrowserStartError(RetryableError):
    """Chrome/chromedriver could not be started."""


class LoginError(TerminalError):
    """Authentication against the booking site failed."""


class NavigationError(RetryableError):
    """Could not reach the reservations page."""


class ElementNotFoundError(RetryableError):
    """A required element never appeared."""


class BookingError(RetryableError):
    """The booking flow failed in a way that may be worth retrying."""

