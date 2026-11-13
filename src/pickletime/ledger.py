"""A small on-disk record of what has already been booked.

A run can repeat: the owner says "book now" after the routine already booked,
a routine fires twice, or a run that succeeded trips over a follow-up error and
is tried again. Replaying a *successful* booking run is how you end up with two
courts and one wallet, so this ledger short-circuits a repeat run for a court
date that is already booked, whatever caused the repeat.

On Grok Bot the file lives under ``/workspace/pickletime-desk/state``, which
survives the computer's sleeps (``PICKLETIME_LEDGER_PATH``, set by
``scripts/env.sh``). It is deliberately best-effort: a ledger that cannot be
written logs a warning and gets out of the way rather than blocking a booking.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger()

__all__ = ["BookingLedger", "NullLedger", "LedgerEntry", "build_ledger"]


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    recorded_at: datetime
    payload: dict[str, Any]


class NullLedger:
    """No-op ledger, for tests and for anyone who opts out."""

    def lookup(self, key: str) -> LedgerEntry | None:  # noqa: ARG002 - interface shape
        return None

    def record(self, key: str, payload: dict[str, Any]) -> None:  # noqa: ARG002 - interface shape
        return None


class BookingLedger:
    """JSON file mapping booking key -> what we recorded, with a TTL."""

    def __init__(self, path: str | Path, *, ttl_hours: int = 36) -> None:
        self.path = Path(path)
        self.ttl = timedelta(hours=ttl_hours)

    @staticmethod
    def key_for(organization_id: int, sport_id: int, court_date: str) -> str:
        return f"{organization_id}:{sport_id}:{court_date}"

    # -- reads ------------------------------------------------------------
    def lookup(self, key: str) -> LedgerEntry | None:
        """Return a live entry for ``key``, or ``None`` if absent or expired."""
        for entry in self._load():
            if entry.key != key:
                continue
            if self.ttl.total_seconds() > 0 and self._age(entry) > self.ttl:
                return None
            return entry
        return None

    # -- writes -----------------------------------------------------------
    def record(self, key: str, payload: dict[str, Any]) -> None:
        entries = [e for e in self._load() if e.key != key and not self._expired(e)]
        entries.append(
            LedgerEntry(key=key, recorded_at=datetime.now(UTC), payload=payload)
        )
        self._save(entries)

    # -- internals --------------------------------------------------------
    def _age(self, entry: LedgerEntry) -> timedelta:
        return datetime.now(UTC) - entry.recorded_at

    def _expired(self, entry: LedgerEntry) -> bool:
        return self.ttl.total_seconds() > 0 and self._age(entry) > self.ttl

    def _load(self) -> list[LedgerEntry]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Ledger at {self.path} unreadable ({exc}); starting fresh")
            return []

        entries: list[LedgerEntry] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                entries.append(
                    LedgerEntry(
                        key=str(item["key"]),
                        recorded_at=datetime.fromisoformat(item["recorded_at"]),
                        payload=dict(item.get("payload") or {}),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def _save(self, entries: list[LedgerEntry]) -> None:
        serialised = [
            {
                "key": e.key,
                "recorded_at": e.recorded_at.isoformat(),
                "payload": e.payload,
            }
            for e in entries
        ]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot corrupt the ledger.
            handle, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".ledger-", suffix=".json"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(serialised, fh)
            Path(tmp_path).replace(self.path)
        except OSError as exc:
            logger.warning(f"Could not persist ledger to {self.path}: {exc}")


def build_ledger(path: str | None, ttl_hours: int) -> BookingLedger | NullLedger:
    """A real ledger when a path is configured, otherwise a no-op."""
    if not path:
        return NullLedger()
    return BookingLedger(path, ttl_hours=ttl_hours)
