"""PickleTime Bot on Grok Bot: the one program a routine or the owner runs.

Always started through ``scripts/run.sh``, which puts the engine's venv and the
paths from ``env.sh`` in place. Subcommands:

  book [--slot HH:MM] [--detach]   the daily routine: skip off-days at once,
                                   otherwise wait until shortly before
                                   release_time, then book (or rehearse)
  report                           today's outcome, for a detached run
  status                           mode, next play dates, recent runs, ledger
  timing                           how late routines actually started
  schedule show|propose|apply|discard
                                   your schedule in plain words; a change from
                                   interview answers (JSON); apply needs the tap
  capture [--open-slot]            log in and record the site's structure,
                                   scrubbed, for fixing the engine; never books
  doctor [--full]                  readiness checks; --full also books a court
                                   on a local fake CourtReserve to prove Chrome
  go-live --owner-tapped           switch from rehearsal (dry runs) to live
  rehearse --owner-tapped          switch back to dry runs

Why the wait lives here and not in the routine schedule: Grok Bot routines
have been reported to start 10-37 minutes late. So the routine fires well
before release (default 45 min) and this program holds until ``lead_seconds``
before ``release_time``, then hands over to the booking engine
(``src/pickletime``), which does its own
warm-up and fires on the second.

Output is written for a chat: a few plain lines, the first one the verdict.
The detailed engine log goes to ``$PT_LOGS``, never to the chat.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

EXIT_OK = 0  # booked, rehearsed, skipped or quiet
EXIT_NOT_BOOKED = 1
EXIT_CONFIG = 2
EXIT_INFRA = 3

DEFAULT_LEAD_SECONDS = 180
DEFAULT_MAX_WAIT_MINUTES = 120


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


DESK = _env_path("PT_DESK", "/workspace/pickletime-desk")
STATE = _env_path("PT_STATE", str(DESK / "state"))
LOGS = _env_path("PT_LOGS", str(DESK / "logs"))
CONFIG = _env_path("PICKLETIME_CONFIG", str(DESK / "config.yaml"))
SECRETS_FILE = _env_path(
    "PT_SECRETS_FILE", "/workspace/.pickletime-secrets/courtreserve.env"
)
RUNS = STATE / "runs"
CHECKOUT = Path(os.environ.get("PT_CHECKOUT", Path(__file__).resolve().parent.parent))
MODE_FILE = STATE / "mode"


def _checkout_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def read_mode() -> str:
    try:
        mode = MODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "rehearsal"
    return mode if mode in {"rehearsal", "live"} else "rehearsal"


# ---------------------------------------------------------------------------
# Pure planning helpers (unit-tested; no engine import)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WaitPlan:
    """What to do between the routine starting and handing over to the engine."""

    action: str  # "wait", "run-now", "too-early"
    fire_at: datetime | None
    release_at: datetime | None
    wait_seconds: float
    late_for_release: bool


def plan_wait(
    now: datetime,
    release_at: datetime | None,
    *,
    lead_seconds: int = DEFAULT_LEAD_SECONDS,
    max_wait_minutes: int = DEFAULT_MAX_WAIT_MINUTES,
) -> WaitPlan:
    """Decide how long to hold before starting the engine.

    ``release_at`` is today's release moment (None: the club has no fixed
    release, so book at once).
    """
    if release_at is None:
        return WaitPlan("run-now", None, None, 0.0, late_for_release=False)
    fire_at = release_at - timedelta(seconds=lead_seconds)
    wait = (fire_at - now).total_seconds()
    if wait <= 0:
        return WaitPlan(
            "run-now", fire_at, release_at, 0.0, late_for_release=now >= release_at
        )
    if wait > max_wait_minutes * 60:
        return WaitPlan("too-early", fire_at, release_at, wait, late_for_release=False)
    return WaitPlan("wait", fire_at, release_at, wait, late_for_release=False)


def lateness_minutes(slot: str | None, started: datetime) -> float | None:
    """Minutes between the routine's scheduled slot (HH:MM, local) and its start."""
    if not slot:
        return None
    try:
        hour, minute = (int(part) for part in slot.split(":", 1))
    except ValueError:
        return None
    scheduled = started.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (started - scheduled).total_seconds() / 60
    # A slot just before midnight and a start just after it.
    if delta < -12 * 60:
        delta += 24 * 60
    return round(delta, 1)


def verdict_line(result: dict[str, Any]) -> str:
    """The first line of the chat message for one run record."""
    rehearsal = result.get("mode") == "rehearsal" or result.get("dry_run")
    prefix = "REHEARSAL (dry run, nothing saved): " if rehearsal else ""
    day = result.get("date_long") or result.get("date") or "?"
    if result.get("outcome") == "skipped":
        return f"quiet - {result.get('detail') or 'not a play day'}"
    if result.get("outcome") == "already-booked":
        return f"quiet - {day} is already booked ({result.get('time')}, {result.get('duration')} min)"
    if result.get("outcome") == "too-early":
        return f"NOT RUN - {result.get('detail')}"
    if result.get("error"):
        return f"{prefix}FAILED for {day}: {result['error']}"
    if result.get("confirmation") == "unverified" and not rehearsal:
        return (
            f"UNVERIFIED {day} at {result.get('time')} for {result.get('duration')} min: the site neither "
            f"confirmed nor refused. Check your reservations in CourtReserve now; the bot stopped so it "
            f"cannot book a second court. ({result.get('detail')})"
        )
    if result.get("booked"):
        what = f"{result.get('time')} for {result.get('duration')} min"
        verb = "would book" if rehearsal else "BOOKED"
        how = ("the site closed the form without an error" if result.get("confirmation") == "inferred"
               else f"{result.get('confirmation')} confirmation")
        return f"{prefix}{verb} {day} at {what} ({how})"
    if rehearsal:
        # The engine reports a dry run as not booked; its detail says what it found.
        return f"{prefix}{day}: {result.get('detail') or 'nothing available in your window'}"
    return f"NOT BOOKED {day}: {result.get('detail') or 'nothing available in your window'}"


def timing_line(result: dict[str, Any]) -> str:
    parts = []
    if result.get("slot"):
        late = result.get("late_minutes")
        late_txt = "" if late is None else f" ({late:+.0f} min vs schedule)"
        parts.append(f"routine slot {result['slot']}, started {result.get('started_local', '?')}{late_txt}")
    if result.get("release_local"):
        parts.append(f"release {result['release_local']}")
    t = result.get("timings_seconds") or {}
    if t:
        keys = ("chrome", "login", "navigate", "select_date", "armed_wait", "sprint", "total")
        parts.append(" ".join(f"{k}={t[k]:.1f}s" for k in keys if k in t))
    return " | ".join(parts)


def summarize_timing(records: list[dict[str, Any]]) -> dict[str, Any]:
    late = [r["late_minutes"] for r in records if isinstance(r.get("late_minutes"), int | float)]
    finished = [r for r in records if r.get("finished_at")]
    unfinished = [r for r in records if r.get("started_at") and not r.get("finished_at")]
    missed = [r for r in records if r.get("late_for_release")]
    out: dict[str, Any] = {
        "runs": len(records),
        "finished": len(finished),
        "unfinished": len(unfinished),
        "started_after_release": len(missed),
    }
    if late:
        out.update(
            late_min=min(late),
            late_median=statistics.median(late),
            late_max=max(late),
        )
        # Recommend firing early enough for the worst lateness seen, plus the
        # engine lead and a margin; never less than 45 minutes.
        out["recommended_offset_minutes"] = max(45, int(max(late)) + 3 + 10)
    return out


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(UTC)


def _record_path(started: datetime) -> Path:
    return RUNS / f"{started.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def load_records(limit: int | None = None) -> list[dict[str, Any]]:
    if not RUNS.is_dir():
        return []
    files = sorted(RUNS.glob("*.json"))
    if limit:
        files = files[-limit:]
    out = []
    for file in files:
        try:
            out.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Engine glue (imports the booking engine lazily)
# ---------------------------------------------------------------------------
def load_secrets_file() -> None:
    """Fallback credential source: KEY=VALUE lines in a 0600 file.

    The secure secret card (USERID / PASSWORD) is preferred and always wins.
    """
    if os.environ.get("USERID") and os.environ.get("PASSWORD"):
        return
    if not SECRETS_FILE.is_file():
        return
    mode = SECRETS_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(
            f"FAILED: {SECRETS_FILE} is readable by others (mode {mode:o}); run chmod 600 on it"
        )
    for raw in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in {"USERID", "PASSWORD"} and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


def _configure_engine_logging(tag: str) -> Path:
    from pickletime.logging_setup import configure_logging

    LOGS.mkdir(parents=True, exist_ok=True)
    log_file = LOGS / f"{_now_utc():%Y%m%dT%H%M%SZ}-{tag}.log"
    configure_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_output=False,
        log_dir=None,
        secrets=[os.environ.get("PASSWORD")],
    )
    # configure_logging writes to stderr; keep the chat clean by sending the
    # engine's log to a file instead.
    from loguru import logger

    logger.remove()
    logger.add(str(log_file), level=os.environ.get("LOG_LEVEL", "INFO"), enqueue=False)
    return log_file


def _raw_config(path: Path | None = None) -> dict[str, Any]:
    import yaml

    target = path or CONFIG
    if not target.is_file():
        return {}
    loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _load_settings(*, dry_run: bool | None, require_credentials: bool = True) -> Any:
    from pickletime.errors import ConfigurationError
    from pickletime.schedule import has_facility
    from pickletime.settings import load_settings

    # The engine has a default club; a schedule must name its own, or a fresh
    # bot would book at whichever club the defaults point to.
    if not has_facility(_raw_config()):
        raise ConfigurationError(
            f"{CONFIG} names no club yet. Say 'set up my schedule' to choose your club, days and times."
        )
    overrides = {"dry_run": True} if dry_run else None
    return load_settings(CONFIG, overrides, require_credentials=require_credentials)


# ---------------------------------------------------------------------------
# book
# ---------------------------------------------------------------------------
def cmd_book(args: argparse.Namespace) -> int:
    if args.detach:
        return _detach(args)

    started = _now_utc()
    mode = read_mode()
    record: dict[str, Any] = {
        "started_at": started.isoformat(),
        "slot": args.slot,
        "mode": mode,
        "release": os.environ.get("BOT_RELEASE"),
        "bot_commit": _checkout_commit(),
    }
    path = _record_path(started)

    try:
        load_secrets_file()
        log_file = _configure_engine_logging("book")
        record["log"] = str(log_file)
        settings = _load_settings(dry_run=(mode != "live") or args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:  # ConfigurationError and friends
        record.update(outcome="config-error", error=str(exc).splitlines()[0], finished_at=_now_utc().isoformat())
        _write_record(path, record)
        print(f"FAILED: configuration or credentials: {record['error']}")
        print("Say 'run the doctor' to see which check fails.")
        return EXIT_CONFIG

    from pickletime.ledger import BookingLedger, build_ledger
    from pickletime.timeutil import format_long_date, format_us_date

    booking = settings.booking
    tz = booking.tz
    local_start = started.astimezone(tz)
    record["started_local"] = f"{local_start:%a %H:%M}"
    record["late_minutes"] = lateness_minutes(args.slot, local_start)

    target: date = booking.target_date()
    effective = booking.for_date(target)
    date_label = format_us_date(target)
    record.update(date=date_label, date_long=f"{target:%a %d %b %Y}", dry_run=effective.dry_run)

    # 1. Off-day: decide now, before any waiting, so most days cost seconds.
    rotation = effective.rotation
    if rotation is not None and not rotation.is_play_date(target):
        record.update(
            outcome="skipped",
            detail=(
                f"{target:%a %d %b} is cycle day {rotation.offset_of(target)}, "
                f"not one of your play days {rotation.play_on_offsets}"
            ),
            finished_at=_now_utc().isoformat(),
        )
        _write_record(path, record)
        print(verdict_line(record))
        return EXIT_OK

    # 2. Already booked (the ledger survives in /workspace).
    ledger = build_ledger(settings.runtime.ledger_path, settings.runtime.ledger_ttl_hours)
    existing = ledger.lookup(BookingLedger.key_for(effective.organization_id, effective.sport_id, date_label))
    if existing is not None and not args.ignore_ledger:
        record.update(
            outcome="already-booked",
            time=existing.payload.get("time"),
            duration=existing.payload.get("duration"),
            finished_at=_now_utc().isoformat(),
        )
        _write_record(path, record)
        print(verdict_line(record))
        return EXIT_OK

    # 3. Hold until shortly before release.
    # Today's release moment. A routine that starts after it runs at once:
    # The engine sprints immediately when the release has passed.
    release_at = None
    if effective.release_time is not None:
        release_at = datetime.combine(local_start.date(), effective.release_time, tzinfo=tz)
        record["release_local"] = f"{release_at:%H:%M:%S %Z}"
    plan = plan_wait(
        local_start, release_at, lead_seconds=args.lead_seconds, max_wait_minutes=args.max_wait_minutes
    )
    record["late_for_release"] = plan.late_for_release
    if plan.action == "too-early":
        record.update(
            outcome="too-early",
            detail=(
                f"started {plan.wait_seconds / 60:.0f} min before the engine is due "
                f"(cap {args.max_wait_minutes} min); move the routine later"
            ),
            finished_at=_now_utc().isoformat(),
        )
        _write_record(path, record)
        print(verdict_line(record))
        return EXIT_CONFIG

    record["outcome"] = "waiting"
    _write_record(path, record)  # visible to 'report' while we wait
    if plan.action == "wait" and plan.fire_at is not None:
        from pickletime.timeutil import sleep_until

        sleep_until(plan.fire_at, max_wait_seconds=plan.wait_seconds + 30)
    record["engine_started_at"] = _now_utc().isoformat()

    # 4. Book. The engine does its own warm-up, arms on release_time and sprints.
    from pickletime.bot import CourtBookingBot

    kwargs: dict[str, Any] = {}
    if args.ignore_ledger:
        from pickletime.ledger import NullLedger

        kwargs["ledger"] = NullLedger()
    record["outcome"] = "running"
    _write_record(path, record)
    try:
        report = CourtBookingBot(settings, **kwargs).run()
        result = report.as_dict()
    except Exception as exc:  # the engine catches its own; this is belt and braces
        result = {"booked": False, "error": f"{type(exc).__name__}: {exc}"}
    record.update(result)
    record["date_long"] = format_long_date(target)
    record["outcome"] = (
        "skipped" if result.get("skipped_reason") == "outside the rotation"
        else "already-booked" if result.get("skipped_reason")
        else "error" if result.get("error")
        else "booked" if result.get("booked")
        else "unverified" if result.get("confirmation") == "unverified"
        else "not-booked"
    )
    record["finished_at"] = _now_utc().isoformat()
    _write_record(path, record)

    print(verdict_line(record))
    print(timing_line(record))
    tried = _attempt_lines(record.get("log"))
    if tried and record["outcome"] not in {"skipped", "already-booked"}:
        print("Tried: " + "; ".join(tried[:12]) + (f"; ... {len(tried) - 12} more" if len(tried) > 12 else ""))
    for answer in _after_confirm_lines(record.get("log"))[:5]:
        print(f"Site's answer after Confirm (share this line when asked): {answer}")
    if plan.late_for_release:
        print(
            "WARNING: this routine started after the release; slots may already be gone. "
            "Say 'show routine timing' and move the routine earlier."
        )
    if record["outcome"] == "error":
        print(f"Engine log: {record.get('log')}")
        return EXIT_INFRA
    return EXIT_OK if record["outcome"] in {"booked", "skipped", "already-booked"} or effective.dry_run else EXIT_NOT_BOOKED


def _log_lines(log: str | None, marker: str) -> list[str]:
    if not log:
        return []
    try:
        lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.split(marker, 1)[1].strip() for line in lines if marker in line]


def _after_confirm_lines(log: str | None) -> list[str]:
    """What the site showed after each Confirm the engine pressed."""
    return [line[:1500] for line in _log_lines(log, "AFTER CONFIRM")]


def _attempt_lines(log: str | None) -> list[str]:
    """One short entry per attempt: '6:30 PM / 60min unavailable - no open slot at that time'."""
    out = []
    for line in _log_lines(log, "ATTEMPT "):
        head, _, rest = line.partition(": ")
        out.append(f"{head} {rest}".strip()[:140])
    return out


def _detach(args: argparse.Namespace) -> int:
    """Start the booking in its own session and return at once.

    For computers where a routine's command is cut off before a long wait
    finishes. The 'report' routine posts the outcome afterwards.
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / f"{_now_utc():%Y%m%dT%H%M%SZ}-detached.out"
    argv = [sys.executable, str(Path(__file__).resolve()), "book"]
    if args.slot:
        argv += ["--slot", args.slot]
    argv += ["--lead-seconds", str(args.lead_seconds), "--max-wait-minutes", str(args.max_wait_minutes)]
    with out.open("ab") as sink:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            argv, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
        )
    print(f"armed in the background (pid {proc.pid}); the report routine will post the outcome")
    return EXIT_OK


# ---------------------------------------------------------------------------
# report / status / timing
# ---------------------------------------------------------------------------
def cmd_report(_args: argparse.Namespace) -> int:
    records = load_records(limit=10)
    since = _now_utc() - timedelta(hours=18)
    todays = [r for r in records if datetime.fromisoformat(r["started_at"]) >= since]
    if not todays:
        print("UNAVAILABLE - no booking run started in the last 18 hours; check that the book routine ran")
        return EXIT_INFRA
    last = todays[-1]
    if not last.get("finished_at"):
        print(
            f"UNAVAILABLE - today's run is still '{last.get('outcome')}' or was cut off; "
            f"engine log: {last.get('log')}"
        )
        return EXIT_INFRA
    print(verdict_line(last))
    if last.get("outcome") not in {"skipped", "already-booked"}:
        print(timing_line(last))
    return EXIT_OK


def cmd_capture(args: argparse.Namespace) -> int:
    """Log in and record the booking site's structure, scrubbed. Never books."""
    started = _now_utc()
    out_dir = DESK / "captures" / f"{started:%Y%m%dT%H%M%SZ}"
    try:
        load_secrets_file()
        _configure_engine_logging("capture")
        settings = _load_settings(dry_run=True)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FAILED: configuration or credentials: {str(exc).splitlines()[0]}")
        return EXIT_CONFIG

    from pickletime.capture import capture_site

    try:
        result = capture_site(settings, out_dir, open_slot=args.open_slot)
    except Exception as exc:
        print(f"FAILED: the capture stopped: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return EXIT_INFRA
    failed = [s["step"] for s in result.get("steps", []) if not s["ok"]]
    print(f"Capture written to {out_dir} (nothing was booked; text is scrubbed).")
    print(f"Steps failed: {', '.join(failed) if failed else 'none'}. Screenshots: {', '.join(sorted(p.name for p in out_dir.glob('*.png')))}")
    print("Share summary.txt below with whoever is fixing the engine; screenshots are not scrubbed.")
    print()
    print((out_dir / "summary.txt").read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_status(_args: argparse.Namespace) -> int:
    print(f"mode: {read_mode()}  (rehearsal = dry runs only; live = real bookings)")
    print(f"release: {os.environ.get('BOT_RELEASE')}  commit: {_checkout_commit()[:12]}")
    try:
        load_secrets_file()
        settings = _load_settings(dry_run=None, require_credentials=False)
        booking = settings.booking
        from pickletime.timeutil import now_in

        today = now_in(booking.tz).date()
        print(f"release_time: {booking.release_time}  timezone: {booking.timezone}  days_in_advance: {booking.days_in_advance}")
        if booking.rotation is not None:
            print("next play dates:")
            # Dates whose booking day is still ahead, so every "fires" is upcoming.
            first = today + timedelta(days=booking.days_in_advance)
            for line in booking.rotation.calendar(after=first, count=6, days_in_advance=booking.days_in_advance):
                print(f"  {line}")
    except Exception as exc:
        print(f"config: FAILED to load: {str(exc).splitlines()[0]}")
    recent = load_records(limit=5)
    if recent:
        print("recent runs:")
        for rec in recent:
            print(f"  {rec.get('started_at', '?')[:16]}  {verdict_line(rec)}")
    return EXIT_OK


def cmd_timing(_args: argparse.Namespace) -> int:
    records = [r for r in load_records(limit=60) if r.get("slot")]
    if not records:
        print("quiet - no routine runs recorded yet")
        return EXIT_OK
    s = summarize_timing(records)
    print(
        f"{s['runs']} routine run(s): {s['finished']} finished, {s['unfinished']} cut off or still running, "
        f"{s['started_after_release']} started after release"
    )
    if "late_max" in s:
        print(
            f"lateness vs schedule: min {s['late_min']:+.0f}, median {s['late_median']:+.0f}, "
            f"max {s['late_max']:+.0f} min"
        )
        print(f"recommended: fire the book routine {s['recommended_offset_minutes']} min before release_time")
    if s["unfinished"]:
        print("Runs were cut off: switch the book routine to --detach and add the report routine (SETUP.md 8).")
    return EXIT_OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def release_ref(checkout: Path, release: str, head: str) -> str | None:
    """Which published ref of ``release`` the checkout is at: its tag or its release branch.

    Releases are published as a tag ``vX.Y.Z`` or a branch ``release/vX.Y.Z``
    that never moves once published; either pins the code that books.
    """
    if not release:
        return None
    for ref in (release, f"release/{release}", f"origin/release/{release}"):
        found = subprocess.run(
            ["git", "-C", str(checkout), "rev-list", "-n1", ref, "--"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if found and found == head:
            return "tag" if ref == release else "release branch"
    return None


def _check(results: list[tuple[str, str, str]], name: str, ok: bool | None, detail: str) -> None:
    status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
    results.append((status, name, detail))


def cmd_doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []
    checkout = CHECKOUT
    release = os.environ.get("BOT_RELEASE", "")

    # 1. Release pin of this checkout.
    try:
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.strip()
        via = release_ref(checkout, release, head)
        if dirty:
            _check(results, "release_pin", False, f"checkout has uncommitted changes: {len(dirty.splitlines())} path(s)")
        elif via:
            _check(results, "release_pin", True, f"release {release} ({via}) at {head[:12]}, clean")
        else:
            _check(results, "release_pin", None, f"HEAD {head[:12]} is not release {release} (development checkout)")
    except (OSError, subprocess.CalledProcessError) as exc:
        _check(results, "release_pin", False, f"not a git checkout: {exc}")

    # 2. Engine: the venv holds the code of this checkout, not an older one.
    try:
        import pickletime

        installed = (Path(sys.prefix) / ".bot-commit").read_text(encoding="utf-8").strip()
        want = _checkout_commit()
        _check(
            results, "engine", installed == want,
            f"booking engine {pickletime.__version__} from {installed[:12]} (checkout {want[:12]}), "
            f"Python {sys.version.split()[0]}" + ("" if installed == want else "; run scripts/install.sh"),
        )
    except Exception as exc:
        _check(results, "engine", False, f"not installed: {exc}; run scripts/install.sh")

    # 3. Chrome binaries.
    chrome = Path(os.environ.get("CHROME_BIN", ""))
    driver = Path(os.environ.get("CHROME_DRIVER_PATH", ""))
    if chrome.is_file() and driver.is_file():
        version = subprocess.run([str(chrome), "--version"], capture_output=True, text=True, check=False).stdout.strip()
        dversion = subprocess.run([str(driver), "--version"], capture_output=True, text=True, check=False).stdout.strip()
        same = version.split()[-1:] == dversion.split()[1:2]
        _check(results, "chrome", bool(version) and same, f"{version or 'chrome will not run'} / {dversion or 'driver will not run'}")
    else:
        _check(results, "chrome", False, "Chrome for Testing missing; run scripts/install.sh")

    # 4. Config.
    from pickletime.schedule import has_facility

    settings = None
    if not has_facility(_raw_config()):
        _check(results, "config", None, "no club set yet; say 'set up my schedule' (nothing can book until then)")
    else:
        try:
            settings = _load_settings(dry_run=None, require_credentials=False)
            b = settings.booking
            _check(results, "config", True, f"{CONFIG}: club {b.organization_id}, sport {b.sport_id}, release "
                   f"{b.release_time}, {b.timezone}, {b.days_in_advance} days ahead")
        except Exception as exc:
            _check(results, "config", False, str(exc).splitlines()[0])

    # 5. Credentials.
    try:
        load_secrets_file()
    except SystemExit as exc:
        _check(results, "credentials", False, str(exc))
    else:
        user, pw = os.environ.get("USERID"), os.environ.get("PASSWORD")
        if user and pw:
            from pickletime.settings import Credentials

            src = "secret card" if not SECRETS_FILE.is_file() else "secret card or file"
            _check(results, "credentials", True, f"USERID {Credentials(username=user, password=pw).masked_username} ({src})")
        else:
            _check(results, "credentials", False, "USERID and PASSWORD not set; add them with the secure secret card")

    # 6. State.
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        probe = STATE / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _check(results, "state", True, f"{STATE} writable; mode {read_mode()}")
    except OSError as exc:
        _check(results, "state", False, f"{STATE} not writable: {exc}")

    # 7. Full: real Chrome against a local fake CourtReserve (no live site).
    if args.full:
        _configure_engine_logging("doctor")
        try:
            ok, detail = _fake_site_rehearsal()
        except Exception as exc:  # a crash here is a failed check, not a traceback
            ok, detail = False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        _check(results, "fake_site_booking", ok, detail)
        try:
            ok, detail = _fake_portal_rehearsal()
        except Exception as exc:  # a crash here is a failed check, not a traceback
            ok, detail = False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        _check(results, "fake_portal_booking", ok, detail)

    for status, name, detail in results:
        print(f"[{status}] {name}: {detail}")
    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    print(f"verdict: {'FAIL' if fails else 'PASS'} ({len(results)} checks; {warns} warning(s), {fails} failure(s))")
    return EXIT_INFRA if fails else EXIT_OK


def _fake_site_rehearsal() -> tuple[bool, str]:
    """Book a court on the test suite's fake CourtReserve with the installed Chrome."""
    sys.path.insert(0, str(CHECKOUT / "tests" / "engine"))
    try:
        from fake_site import FakeCourtReserve, SiteState  # type: ignore[import-not-found]

        from pickletime.bot import CourtBookingBot
        from pickletime.ledger import NullLedger
        from pickletime.settings import BookingSettings, Credentials, RuntimeSettings, Settings
        from pickletime.timeutil import format_long_date, format_us_date
    except Exception as exc:
        return False, f"cannot load the fake site: {exc}"

    base = {
        "search_start_time": "10:00 AM", "search_end_time": "12:00 PM", "preferred_durations": [60, 30],
        "booking_priority": "duration", "priority_time_slots": ["11:30 AM"], "headless": True,
        "webdriver_timeout": 10, "page_load_timeout": 30, "poll_interval_ms": 50, "sprint_seconds": 8,
        "max_retries": 0, "retry_interval": 0, "chrome_binary": os.environ.get("CHROME_BIN"),
        "chrome_driver_path": os.environ.get("CHROME_DRIVER_PATH"),
    }
    state = SiteState(slots={"11:30 AM": ["30 minutes", "1 hour"]})
    creds = Credentials(username=state.username, password=state.password)
    runtime = RuntimeSettings(
        log_level="WARNING", log_json=False, ledger_path=str(STATE / "fake-site-ledger.json")
    )
    target = BookingSettings(**base).target_date()
    titles = [format_long_date(target + timedelta(days=o)) for o in (-1, 0, 1)]
    started = _time.monotonic()
    with FakeCourtReserve(state, titles, format_us_date(target)) as site:
        settings = Settings(booking=BookingSettings(**base, base_url=site.base_url), credentials=creds, runtime=runtime)
        report = CourtBookingBot(settings, ledger=NullLedger()).run()
    took = _time.monotonic() - started
    if report.booked and state.bookings == [{"slot": "11:30 AM", "duration": "1 hour"}]:
        return True, f"booked 11:30 AM for 60 min on the fake site in {took:.1f}s (Chrome works here)"
    return False, f"fake-site booking failed: {report.error or report.detail}; bookings={state.bookings}"


def _fake_portal_rehearsal() -> tuple[bool, str]:
    """Book on the test suite's fake of the current member portal (Reserve-button grid)."""
    sys.path.insert(0, str(CHECKOUT / "tests" / "engine"))
    from fake_portal import PortalState  # type: ignore[import-not-found]
    from fake_portal_grid import FakePortalGrid  # type: ignore[import-not-found]

    from pickletime.bot import CourtBookingBot
    from pickletime.ledger import NullLedger
    from pickletime.settings import BookingSettings, Credentials, RuntimeSettings, Settings

    base = {
        "search_start_time": "6:00 PM", "search_end_time": "7:00 PM", "preferred_durations": [60, 30],
        "priority_time_slots": ["6:30 PM"], "headless": True, "webdriver_timeout": 10, "page_load_timeout": 30,
        "poll_interval_ms": 50, "sprint_seconds": 8, "max_retries": 0, "retry_interval": 0, "days_in_advance": 8,
        "chrome_binary": os.environ.get("CHROME_BIN"), "chrome_driver_path": os.environ.get("CHROME_DRIVER_PATH"),
    }
    target = BookingSettings(**base).target_date()
    state = PortalState(
        today=target - timedelta(days=8), form_delay_ms=500,
        open_slots={target: {"6:30 PM": 1, "7:00 PM": 1}},
    )
    runtime = RuntimeSettings(log_level="WARNING", log_json=False, ledger_path=str(STATE / "fake-portal-ledger.json"))
    started = _time.monotonic()
    with FakePortalGrid(state) as site:
        settings = Settings(booking=BookingSettings(**base, base_url=site.base_url),
                            credentials=Credentials(username=state.username, password=state.password), runtime=runtime)
        report = CourtBookingBot(settings, ledger=NullLedger()).run()
    took = _time.monotonic() - started
    if report.booked and state.bookings == [{"date": target.isoformat(), "slot": "6:30 PM", "duration": "60"}]:
        return True, f"stepped 8 days and booked 6:30 PM for 60 min on the fake portal in {took:.1f}s"
    return False, f"fake-portal booking failed: {report.error or report.detail}; bookings={state.bookings}"


# ---------------------------------------------------------------------------
# schedule: show, propose, apply, discard
# ---------------------------------------------------------------------------
PROPOSALS = DESK / "proposals"
BACKUPS = DESK / "backups"
PROPOSED = PROPOSALS / "config.proposed.yaml"


def _today() -> Any:
    from pickletime.timeutil import get_timezone, now_in

    tz = _raw_config().get("timezone") or "America/Los_Angeles"
    return now_in(get_timezone(str(tz))).date()


def cmd_schedule(args: argparse.Namespace) -> int:
    return {"show": _schedule_show, "propose": _schedule_propose, "apply": _schedule_apply,
            "discard": _schedule_discard}[args.action](args)


def _schedule_show(_args: argparse.Namespace) -> int:
    from pickletime.schedule import describe

    print(f"mode: {read_mode()}  (rehearsal = dry runs only; live = real bookings)")
    try:
        for line in describe(_raw_config(), _today()):
            print(line)
    except Exception as exc:
        print(f"FAILED: {CONFIG} is not a valid schedule: {str(exc).splitlines()[0]}")
        return EXIT_CONFIG
    if PROPOSED.is_file():
        print("A proposed change is waiting: say 'apply my schedule' or 'discard the proposal'.")
    return EXIT_OK


def _schedule_propose(args: argparse.Namespace) -> int:
    from pickletime.schedule import ScheduleError, build_config, config_diff, describe, dump_config

    raw = args.answers
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        answers = json.loads(raw)
        if not isinstance(answers, dict):
            raise ValueError("answers must be a JSON object")
    except ValueError as exc:
        print(f"FAILED: the answers are not valid JSON: {exc}")
        return EXIT_CONFIG

    current = _raw_config()
    try:
        config, warnings = build_config(current, answers)
    except ScheduleError as exc:
        print(f"FAILED: {exc}")
        return EXIT_CONFIG

    text = dump_config(config)
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    PROPOSED.write_text(text, encoding="utf-8")
    print("PROPOSED schedule (not in use yet):")
    for line in describe(config, _today()):
        print(f"  {line}")
    for warning in warnings:
        print(f"note: {warning}")
    changes = config_diff(CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else "", text)
    if changes:
        print("changes to config.yaml:")
        for line in changes[:60]:
            print(f"  {line}")
    else:
        print("no change from the current schedule")
    print("Say 'apply my schedule' to use it, or 'discard the proposal'.")
    return EXIT_OK


def _schedule_apply(args: argparse.Namespace) -> int:
    from pickletime.schedule import build_config, describe, routine_slot

    if not args.owner_tapped:
        print("refused: changing the schedule needs the owner's approval (--owner-tapped after their tap)")
        return EXIT_CONFIG
    if not PROPOSED.is_file():
        print("FAILED: no proposed schedule; say 'change my schedule' first")
        return EXIT_CONFIG
    proposed = _raw_config(PROPOSED)
    try:
        build_config(proposed, {})  # re-validate exactly what will be installed
    except Exception as exc:
        print(f"FAILED: the proposal is no longer valid: {exc}")
        return EXIT_CONFIG

    before = _raw_config()
    if CONFIG.is_file():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS / f"config-{_now_utc():%Y%m%dT%H%M%SZ}.yaml"
        backup.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"previous schedule kept in {backup}")
    CONFIG.write_text(PROPOSED.read_text(encoding="utf-8"), encoding="utf-8")
    PROPOSED.unlink()
    print("APPLIED. Your schedule is now:")
    for line in describe(proposed, _today()):
        print(f"  {line}")
    old_slot, new_slot = routine_slot(before.get("release_time")), routine_slot(proposed.get("release_time"))
    if old_slot != new_slot:
        print(f"ACTION: move the daily book routine to {new_slot or 'any time of day'} "
              f"(it was set for {old_slot or 'no release time'}), with --slot {new_slot} in its prompt.")
    return EXIT_OK


def _schedule_discard(_args: argparse.Namespace) -> int:
    if PROPOSED.is_file():
        PROPOSED.unlink()
        print("proposal discarded; the current schedule is unchanged")
    else:
        print("quiet - no proposal to discard")
    return EXIT_OK


# ---------------------------------------------------------------------------
# mode switches (owner authority)
# ---------------------------------------------------------------------------
def _set_mode(args: argparse.Namespace, mode: str) -> int:
    if not args.owner_tapped:
        print("refused: changing the mode needs the owner's approval (--owner-tapped after their tap)")
        return EXIT_CONFIG
    STATE.mkdir(parents=True, exist_ok=True)
    before = read_mode()
    MODE_FILE.write_text(mode + "\n", encoding="utf-8")
    with (STATE / "mode-history.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{_now_utc().isoformat()} {before} -> {mode} (owner tapped)\n")
    print(f"mode: {before} -> {mode}")
    if mode == "live":
        print("From the next book routine on, a found slot is really booked.")
    return EXIT_OK


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ptbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    book = sub.add_parser("book", help="the daily booking routine")
    book.add_argument("--slot", help="the routine's scheduled time HH:MM (for lateness tracking)")
    book.add_argument("--detach", action="store_true", help="arm in the background and return")
    book.add_argument("--dry-run", action="store_true", help="rehearse even in live mode")
    book.add_argument("--ignore-ledger", action="store_true", help=argparse.SUPPRESS)
    book.add_argument("--lead-seconds", type=int, default=DEFAULT_LEAD_SECONDS)
    book.add_argument("--max-wait-minutes", type=int, default=DEFAULT_MAX_WAIT_MINUTES)
    book.set_defaults(func=cmd_book)

    sub.add_parser("report", help="today's outcome").set_defaults(func=cmd_report)
    sub.add_parser("status", help="mode, play dates, recent runs").set_defaults(func=cmd_status)
    sub.add_parser("timing", help="routine lateness").set_defaults(func=cmd_timing)

    schedule = sub.add_parser("schedule", help="show, propose, apply or discard a schedule")
    schedule.add_argument("action", choices=["show", "propose", "apply", "discard"])
    schedule.add_argument("--answers", default="{}", help="propose: JSON answers, or @file")
    schedule.add_argument("--owner-tapped", action="store_true", help="apply: the owner approved")
    schedule.set_defaults(func=cmd_schedule)

    capture = sub.add_parser("capture", help="record the booking site's structure (never books)")
    capture.add_argument("--open-slot", action="store_true", help="also open one slot's booking form, then close it")
    capture.set_defaults(func=cmd_capture)

    doctor = sub.add_parser("doctor", help="readiness checks")
    doctor.add_argument("--full", action="store_true", help="also book on a local fake site")
    doctor.set_defaults(func=cmd_doctor)

    for name, mode in (("go-live", "live"), ("rehearse", "rehearsal")):
        p = sub.add_parser(name, help=f"switch to {mode} mode (owner only)")
        p.add_argument("--owner-tapped", action="store_true")
        p.set_defaults(func=lambda a, m=mode: _set_mode(a, m))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
