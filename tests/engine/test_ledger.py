"""The double-booking guard."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pickletime.ledger import BookingLedger, NullLedger, build_ledger


def test_records_and_finds_a_booking(tmp_path: Path) -> None:
    ledger = BookingLedger(tmp_path / "ledger.json")
    key = BookingLedger.key_for(13233, 16984, "7/22/2025")

    assert ledger.lookup(key) is None
    ledger.record(key, {"time": "11:30 AM", "duration": 60})

    entry = ledger.lookup(key)
    assert entry is not None
    assert entry.payload["time"] == "11:30 AM"


def test_different_dates_do_not_collide(tmp_path: Path) -> None:
    ledger = BookingLedger(tmp_path / "ledger.json")
    ledger.record(BookingLedger.key_for(1, 2, "7/22/2025"), {"time": "10:00 AM"})

    assert ledger.lookup(BookingLedger.key_for(1, 2, "7/23/2025")) is None
    assert ledger.lookup(BookingLedger.key_for(9, 2, "7/22/2025")) is None


def test_entries_expire(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    stale = datetime.now(UTC) - timedelta(hours=48)
    path.write_text(
        json.dumps([{"key": "k", "recorded_at": stale.isoformat(), "payload": {}}]),
        encoding="utf-8",
    )
    assert BookingLedger(path, ttl_hours=36).lookup("k") is None
    assert BookingLedger(path, ttl_hours=72).lookup("k") is not None


def test_zero_ttl_means_never_expire(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    old = datetime.now(UTC) - timedelta(days=400)
    path.write_text(
        json.dumps([{"key": "k", "recorded_at": old.isoformat(), "payload": {}}]),
        encoding="utf-8",
    )
    assert BookingLedger(path, ttl_hours=0).lookup("k") is not None


def test_recording_twice_keeps_one_entry(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = BookingLedger(path)
    ledger.record("k", {"n": 1})
    ledger.record("k", {"n": 2})

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["payload"]["n"] == 2


def test_corrupt_file_does_not_block_a_booking(tmp_path: Path) -> None:
    """A broken ledger must degrade to 'no record', never to a crash."""
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")

    ledger = BookingLedger(path)
    assert ledger.lookup("k") is None
    ledger.record("k", {"ok": True})
    assert ledger.lookup("k") is not None


def test_malformed_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps([{"key": "good", "recorded_at": datetime.now(UTC).isoformat()},
                    {"nonsense": True}, "string"]),
        encoding="utf-8",
    )
    assert BookingLedger(path).lookup("good") is not None


def test_unwritable_path_is_survivable(tmp_path: Path) -> None:
    """A read-only filesystem warns; it does not stop the booking."""
    blocked = tmp_path / "afile" / "ledger.json"
    (tmp_path / "afile").write_text("I am a file, not a directory", encoding="utf-8")

    ledger = BookingLedger(blocked)
    ledger.record("k", {"ok": True})  # must not raise
    assert ledger.lookup("k") is None


def test_null_ledger_never_remembers() -> None:
    ledger = NullLedger()
    ledger.record("k", {"a": 1})
    assert ledger.lookup("k") is None


def test_build_ledger_switches_on_the_path(tmp_path: Path) -> None:
    assert isinstance(build_ledger(None, 36), NullLedger)
    assert isinstance(build_ledger(str(tmp_path / "l.json"), 36), BookingLedger)
