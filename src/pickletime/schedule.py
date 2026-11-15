"""Turning a person's answers into a booking schedule, and describing one.

The Grok Bot interviews the owner ("which club, which days, what times?") and
hands the answers here as JSON. This module turns them into a validated
``config.yaml``, keeping every advanced key the owner already had, and
describes any schedule in plain words. Nothing here touches the live config:
the runner writes proposals beside it and installs one only after the owner's
tap (``scripts/ptbot.py schedule``).

Answer keys (all optional when changing an existing schedule; a first schedule
needs a facility):

``facility_url``      a CourtReserve link from the club, e.g.
                      ``https://app.courtreserve.com/Online/Reservations/Bookings/12345?sId=678``
``organization_id``, ``sport_id``  the same, given directly
``sport_name``, ``court_type``     what the club calls the sport ("Pickleball")
``timezone``          the club's IANA timezone, e.g. ``America/New_York``
``release_time``      when the club opens the last bookable day ("12:00 PM"), or
                      null if courts open on a rolling basis
``days_in_advance``   how many days ahead the club lets members book
``play_days``         weekday names (["Monday", "Thursday"]) or "every day"
``rotation``          instead of play_days: {"anchor_date": "MM/DD/YYYY",
                      "cycle_days": 14, "play_on_offsets": [3, 7]}
``window``            {"start": "5:30 PM", "end": "7:30 PM"}
``priority_times``    best first, e.g. ["6:30 PM", "7:00 PM"]
``durations``         minutes, best first, e.g. [60, 30]
``weekday_windows``   {"Saturday": {"start": "9:00 AM", "end": "10:30 AM",
                      "priority_times": [...], "durations": [...]}}; null clears
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

__all__ = [
    "ScheduleError",
    "parse_facility_url",
    "weekly_rotation",
    "build_config",
    "describe",
    "routine_slot",
    "config_diff",
    "dump_config",
    "has_facility",
]

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: A Monday, so a 7-day rotation's offsets are weekday numbers (Monday = 0).
WEEKLY_ANCHOR = date(2026, 1, 5)

ANSWER_KEYS = frozenset(
    {
        "facility_url",
        "organization_id",
        "sport_id",
        "sport_name",
        "court_type",
        "timezone",
        "release_time",
        "days_in_advance",
        "play_days",
        "rotation",
        "window",
        "priority_times",
        "durations",
        "weekday_windows",
    }
)

#: Facility-specific keys that must not carry over to a different club.
_FACILITY_SPECIFIC = ("court_type_id", "duration_labels")

#: Key order for a written config: what a person reads first, first.
_ORDER = [
    "organization_id",
    "sport_id",
    "sport_name",
    "court_type",
    "court_type_id",
    "base_url",
    "timezone",
    "release_time",
    "days_in_advance",
    "rotation",
    "search_start_time",
    "search_end_time",
    "time_increment",
    "priority_time_slots",
    "preferred_durations",
    "booking_priority",
    "weekday_overrides",
    "duration_labels",
]

_HEADER = """\
# PickleTime Bot: your booking schedule.
#
# Written by `run.sh schedule apply` from your answers; the previous version is
# kept in /workspace/pickletime-desk/backups/. To change it, tell the Bot
# "change my schedule". Every key, with its default: config/config.sample.yaml.
# Credentials never live here: USERID and PASSWORD come from the secret card.

"""

_DEFAULTS: dict[str, Any] = {
    "sport_name": "Pickleball",
    "court_type": "Pickleball",
    "base_url": "https://app.courtreserve.com",
    "timezone": "America/Los_Angeles",
    "release_time": "12:00 PM",
    "days_in_advance": 7,
    "search_start_time": "6:00 PM",
    "search_end_time": "8:00 PM",
    "time_increment": 30,
    "preferred_durations": [60, 30],
    "booking_priority": "duration",
    "sprint_seconds": 25,
    "release_grace_seconds": 180,
    "poll_interval_ms": 150,
    "max_retries": 1,
    "retry_interval": 5,
    "headless": True,
    "webdriver_timeout": 15,
    "page_load_timeout": 30,
    "block_images": True,
    "dry_run": False,
}


class ScheduleError(ValueError):
    """The answers cannot make a valid schedule; the message says why."""


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------
def parse_facility_url(url: str) -> dict[str, Any]:
    """organization_id, sport_id (when present) and base_url from a club link."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        raise ScheduleError(
            f"{url!r} is not a web link; paste the address of the club's CourtReserve page"
        )
    match = re.search(r"/Online/[A-Za-z]+/[A-Za-z]+/(\d+)", parts.path) or re.search(
        r"/(\d{3,})(?:/|$)", parts.path
    )
    if not match:
        raise ScheduleError(
            f"no club number in {url!r}. Open the club's court booking page in CourtReserve "
            "and copy that address; it looks like .../Online/Reservations/Bookings/12345?sId=678"
        )
    out: dict[str, Any] = {
        "organization_id": int(match.group(1)),
        "base_url": f"{parts.scheme}://{parts.netloc}",
    }
    sport = parse_qs(parts.query).get("sId") or parse_qs(parts.query).get("sid")
    if sport and sport[0].isdigit():
        out["sport_id"] = int(sport[0])
    return out


def weekly_rotation(days: list[str]) -> dict[str, Any]:
    """Weekday names as a 7-day rotation the engine already understands."""
    offsets = []
    for day in days:
        name = _weekday(day)
        offsets.append(WEEKDAYS.index(name))
    if not offsets:
        raise ScheduleError("play_days is empty; name at least one day, or say every day")
    return {
        "anchor_date": WEEKLY_ANCHOR.strftime("%m/%d/%Y"),
        "cycle_days": 7,
        "play_on_offsets": sorted(set(offsets)),
    }


def _weekday(text: str) -> str:
    key = str(text).strip().lower()
    for name in WEEKDAYS:
        if name.lower() == key or name.lower()[:3] == key[:3] and len(key) >= 3:
            return name
    raise ScheduleError(f"{text!r} is not a weekday")


def _clock(text: Any, what: str) -> str:
    from .timeutil import format_clock_time, parse_clock_time

    try:
        return format_clock_time(parse_clock_time(str(text)))
    except Exception as exc:
        raise ScheduleError(f"{what}: {text!r} is not a time like '6:30 PM'") from exc


def routine_slot(release_time: str | None, *, offset_minutes: int = 45) -> str | None:
    """When the daily Grok routine should fire (24-hour HH:MM), or None if no release."""
    if not release_time:
        return None
    moment = datetime.strptime(_clock(release_time, "release_time"), "%I:%M %p") - timedelta(
        minutes=offset_minutes
    )
    return moment.strftime("%H:%M")


def has_facility(config: dict[str, Any] | None) -> bool:
    """Is a club set explicitly? (The engine has defaults; a schedule must not rely on them.)"""
    return bool(config) and "organization_id" in config and "sport_id" in config  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Answers -> config
# ---------------------------------------------------------------------------
def build_config(
    current: dict[str, Any] | None, answers: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """A validated config from the current one (or defaults) plus the answers.

    Returns the config and plain-language warnings. Raises :class:`ScheduleError`
    with a message for the owner when the answers cannot work.
    """
    unknown = sorted(set(answers) - ANSWER_KEYS)
    if unknown:
        raise ScheduleError(f"unknown answer(s): {', '.join(unknown)}")

    base = dict(current or {}) if has_facility(current) else {**_DEFAULTS, **(current or {})}
    config = dict(base)
    warnings: list[str] = []

    # Facility.
    facility: dict[str, Any] = {}
    if answers.get("facility_url"):
        facility.update(parse_facility_url(answers["facility_url"]))
    for key in ("organization_id", "sport_id"):
        if answers.get(key) is not None:
            facility[key] = int(answers[key])
    if facility:
        changed_club = facility.get("organization_id", config.get("organization_id")) != config.get(
            "organization_id"
        )
        config.update(facility)
        if changed_club:
            for key in _FACILITY_SPECIFIC:
                if config.pop(key, None) is not None:
                    warnings.append(f"dropped {key}: it belonged to the previous club")
    if "sport_id" not in config and "organization_id" in config:
        raise ScheduleError(
            "the club link has no sport (no ?sId=...). Open the booking page for your sport "
            "in CourtReserve and copy that address, or give sport_id"
        )
    for key in ("sport_name", "court_type"):
        if answers.get(key):
            config[key] = str(answers[key])
    if answers.get("sport_name") and not answers.get("court_type"):
        config["court_type"] = str(answers["sport_name"])

    if answers.get("timezone"):
        config["timezone"] = str(answers["timezone"])

    # When courts open.
    if "release_time" in answers:
        if answers["release_time"] in (None, "", "none", "rolling"):
            config.pop("release_time", None)
            warnings.append("no release time: each run books as soon as the routine starts")
        else:
            config["release_time"] = _clock(answers["release_time"], "release_time")
    if answers.get("days_in_advance") is not None:
        config["days_in_advance"] = int(answers["days_in_advance"])

    # Which days.
    if answers.get("rotation") is not None and answers.get("play_days") is not None:
        raise ScheduleError("give play_days or rotation, not both")
    if answers.get("play_days") is not None:
        days = answers["play_days"]
        if isinstance(days, str) and days.strip().lower() in {
            "every day",
            "everyday",
            "daily",
            "all",
        }:
            config.pop("rotation", None)
        else:
            config["rotation"] = weekly_rotation([days] if isinstance(days, str) else list(days))
    if answers.get("rotation") is not None:
        config["rotation"] = dict(answers["rotation"])

    # What times.
    window = answers.get("window")
    if window:
        config["search_start_time"] = _clock(window.get("start"), "window start")
        config["search_end_time"] = _clock(window.get("end"), "window end")
    if answers.get("priority_times") is not None:
        config["priority_time_slots"] = [
            _clock(t, "priority time") for t in answers["priority_times"]
        ]
    if answers.get("durations") is not None:
        config["preferred_durations"] = [int(d) for d in answers["durations"]]

    if "weekday_windows" in answers:
        wanted = answers["weekday_windows"]
        if not wanted:
            config.pop("weekday_overrides", None)
        else:
            overrides: dict[str, Any] = {}
            for day, prefs in wanted.items():
                entry: dict[str, Any] = {}
                if prefs.get("start"):
                    entry["search_start_time"] = _clock(prefs["start"], f"{day} start")
                if prefs.get("end"):
                    entry["search_end_time"] = _clock(prefs["end"], f"{day} end")
                if prefs.get("priority_times") is not None:
                    entry["priority_time_slots"] = [
                        _clock(t, f"{day} priority time") for t in prefs["priority_times"]
                    ]
                if prefs.get("durations") is not None:
                    entry["preferred_durations"] = [int(d) for d in prefs["durations"]]
                overrides[_weekday(day)] = entry
            config["weekday_overrides"] = overrides

    if not has_facility(config):
        raise ScheduleError(
            "no club yet: paste the address of your club's court booking page in CourtReserve "
            "(it looks like .../Online/Reservations/Bookings/12345?sId=678)"
        )

    warnings += _validate(config)
    return _ordered(config), warnings


def _validate(config: dict[str, Any]) -> list[str]:
    """Engine validation, plus checks the engine cannot make."""
    from .settings import BookingSettings
    from .timeutil import parse_clock_time

    try:
        settings = BookingSettings(**config)
    except Exception as exc:
        first = str(exc).splitlines()
        detail = next(
            (line.strip() for line in first[1:] if line.strip() and not line.startswith(" ")),
            first[0],
        )
        raise ScheduleError(f"that schedule is not valid: {detail}") from exc

    warnings = []

    def check(start: Any, end: Any, priorities: list[str], label: str) -> None:
        lo, hi = parse_clock_time(str(start)), parse_clock_time(str(end))
        outside = [p for p in priorities if not lo <= parse_clock_time(p) <= hi]
        if outside:
            warnings.append(
                f"{label}: {', '.join(outside)} is outside the window and will never be tried"
            )

    check(
        config["search_start_time"],
        config["search_end_time"],
        config.get("priority_time_slots", []),
        "every day",
    )
    for day, prefs in (config.get("weekday_overrides") or {}).items():
        # Only a day's own priorities: inherited ones outside its window are
        # simply skipped by the engine, which is the point of a day window.
        if prefs.get("priority_time_slots"):
            check(
                prefs.get("search_start_time", config["search_start_time"]),
                prefs.get("search_end_time", config["search_end_time"]),
                prefs["priority_time_slots"],
                day,
            )

    rotation = settings.rotation
    if rotation is not None and rotation.cycle_days == 7:
        days = {WEEKDAYS[o] for o in rotation.play_on_offsets}
        for day in config.get("weekday_overrides") or {}:
            if day not in days:
                warnings.append(f"{day} has its own times but is not a play day")
    return warnings


def _ordered(config: dict[str, Any]) -> dict[str, Any]:
    first = {k: config[k] for k in _ORDER if k in config}
    return {**first, **{k: v for k, v in config.items() if k not in first}}


def dump_config(config: dict[str, Any]) -> str:
    return _HEADER + yaml.safe_dump(
        config, sort_keys=False, default_flow_style=None, allow_unicode=True
    )


def config_diff(old_text: str, new_text: str) -> list[str]:
    """What changed in meaning: comments, key order and quoting are ignored."""

    def canonical(text: str) -> list[str]:
        loaded = yaml.safe_load(text) if text.strip() else {}
        loaded = loaded if isinstance(loaded, dict) else {}
        dumped = yaml.safe_dump(
            _ordered(loaded), sort_keys=False, default_flow_style=None, allow_unicode=True
        )
        return dumped.splitlines()

    return [
        ln
        for ln in difflib.unified_diff(canonical(old_text), canonical(new_text), lineterm="", n=0)
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]


# ---------------------------------------------------------------------------
# Describing
# ---------------------------------------------------------------------------
def describe(config: dict[str, Any], today: date, *, upcoming: int = 6) -> list[str]:
    """A schedule in plain words, with the next play dates and the routine time."""
    from .settings import BookingSettings
    from .timeutil import format_clock_time

    if not has_facility(config):
        return ["No club set yet. Say 'set up my schedule' to choose your club, days and times."]
    s = BookingSettings(**config)
    clock = lambda t: format_clock_time(t)  # noqa: E731
    lines = [
        f"Club: CourtReserve organisation {s.organization_id}, {s.sport_name} "
        f"(sport {s.sport_id}) at {s.base_url}",
    ]
    if s.release_time is not None:
        slot = routine_slot(clock(s.release_time))
        lines.append(
            f"Courts open: {clock(s.release_time)} {s.timezone}, {s.days_in_advance} days ahead "
            f"-> the book routine runs daily at {slot}"
        )
    else:
        lines.append(
            f"Courts open: on a rolling basis, {s.days_in_advance} days ahead "
            "-> book whenever the routine runs"
        )

    if s.rotation is None:
        lines.append("Play days: every day")
    elif s.rotation.cycle_days == 7 and s.rotation.anchor_date.weekday() == 0:
        names = [WEEKDAYS[o] for o in s.rotation.play_on_offsets]
        lines.append(f"Play days: every {', '.join(names)}")
    else:
        lines.append(
            f"Play days: days {', '.join(map(str, s.rotation.play_on_offsets))} of a "
            f"{s.rotation.cycle_days}-day cycle starting {s.rotation.anchor_date:%a %d %b %Y}"
        )

    def times(prefs: Any) -> str:
        pri = ", ".join(prefs.priority_time_slots) or "earliest first"
        return (
            f"{clock(prefs.search_start_time)}-{clock(prefs.search_end_time)}, tries {pri}; "
            f"{' then '.join(str(d) for d in prefs.preferred_durations)} min"
        )

    lines.append(f"Times: {times(s)}")
    for day in WEEKDAYS:
        if day in s.weekday_overrides:
            lines.append(f"  {day}: {times(s.for_date(_next_weekday(today, day)))}")

    lines.append("Next play dates:")
    first = today + timedelta(days=s.days_in_advance)
    shown = 0
    for offset in range(0, 400):
        play = first + timedelta(days=offset)
        if s.rotation is None or s.rotation.is_play_date(play):
            fires = play - timedelta(days=s.days_in_advance)
            prefs = s.for_date(play)
            window = f"{clock(prefs.search_start_time)}-{clock(prefs.search_end_time)}"
            lines.append(f"  {play:%a %d %b %Y}  {window}   (booked on {fires:%a %d %b})")
            shown += 1
            if shown >= upcoming:
                break
    return lines


def _next_weekday(today: date, name: str) -> date:
    return today + timedelta(days=(WEEKDAYS.index(name) - today.weekday()) % 7)
