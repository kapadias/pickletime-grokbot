"""Notifications.

On Grok Bot the chat is the notification: the routine posts each run's result
and Grok's own notifications deliver it to the owner's phone. So the engine's
notifier only logs, and the runner (scripts/ptbot.py) turns the run report into
the chat message.

Everything here is best-effort by construction: a notification must never turn
a successful booking into a failed run, because the court is already reserved.
"""

from __future__ import annotations

from typing import Any, Protocol

from .logging_setup import get_logger
from .settings import BookingSettings

logger = get_logger()

__all__ = ["Notifier", "NullNotifier", "build_notifier"]


class Notifier(Protocol):
    """What the bot needs from a notification backend."""

    def success(self, details: dict[str, Any]) -> None: ...

    def failure(self, reason: str, details: dict[str, Any] | None = None) -> None: ...

    def error(self, message: str, details: dict[str, Any] | None = None) -> None: ...


class NullNotifier:
    """Logs and moves on."""

    def success(self, details: dict[str, Any]) -> None:
        logger.info(f"[notify:success] {_summarise(details)}")

    def failure(self, reason: str, details: dict[str, Any] | None = None) -> None:  # noqa: ARG002
        logger.warning(f"[notify:failure] {reason}")

    def error(self, message: str, details: dict[str, Any] | None = None) -> None:  # noqa: ARG002
        logger.error(f"[notify:error] {message}")


def _summarise(details: dict[str, Any] | None) -> str:
    if not details:
        return "(no details)"
    keys = ("date", "time", "duration", "confirmation", "reason", "error")
    parts = [f"{k}={details[k]}" for k in keys if details.get(k) is not None]
    return ", ".join(parts) or str(details)


def build_notifier(settings: BookingSettings) -> Notifier:  # noqa: ARG001 - one backend today
    return NullNotifier()
