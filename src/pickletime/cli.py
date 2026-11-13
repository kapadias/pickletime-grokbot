"""Command-line entry point.

Exit codes are meaningful so cron/CI can act on them:

===  ============================================
0    A court was booked (or a dry run completed)
1    Ran cleanly, but nothing was available
2    Configuration or credentials are wrong
3    Infrastructure failure (browser, network, site)
===  ============================================
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .bot import CourtBookingBot
from .errors import ConfigurationError
from .logging_setup import configure_logging, get_logger
from .settings import load_dotenv_once, load_settings

logger = get_logger()

EXIT_BOOKED = 0
EXIT_NOT_BOOKED = 1
EXIT_CONFIG = 2
EXIT_INFRA = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pickletime",
        description="Automated court booking for CourtReserve facilities.",
    )
    parser.add_argument("--version", action="version", version=f"pickletime {__version__}")
    parser.add_argument(
        "--config",
        default=os.environ.get("PICKLETIME_CONFIG", "config.yaml"),
        help="Path to the YAML configuration file (default: config.yaml)",
    )

    booking = parser.add_argument_group("booking")
    booking.add_argument("--date", help="Book this exact date (MM/DD/YYYY)")
    booking.add_argument(
        "--days-in-advance",
        type=int,
        help="Book this many days from today, in the facility's timezone",
    )
    booking.add_argument("--start-time", help="Earliest slot to consider, e.g. '6:00 PM'")
    booking.add_argument("--end-time", help="Latest slot to consider, e.g. '9:00 PM'")
    booking.add_argument(
        "--durations", help="Comma-separated minutes in preference order, e.g. '60,30'"
    )
    booking.add_argument(
        "--priority",
        choices=["time", "duration"],
        dest="booking_priority",
        help="Sweep every slot at the best duration first, or every duration at the best slot",
    )

    race = parser.add_argument_group("slot race")
    race.add_argument("--release-time",
                      help="Wall-clock time slots open, e.g. '12:00 PM'. The bot warms up "
                           "beforehand and fires at that instant.")
    race.add_argument("--sprint-seconds", type=int,
                      help="How long to keep re-trying after the release moment")

    browser = parser.add_argument_group("browser")
    headless = browser.add_mutually_exclusive_group()
    headless.add_argument("--headless", action="store_true", default=None,
                          help="Run Chrome without a window (default)")
    headless.add_argument("--no-headless", action="store_false", dest="headless",
                          help="Show the browser — useful when debugging selectors")
    browser.add_argument("--webdriver-timeout", type=int, help="Seconds to wait for elements")

    safety = parser.add_argument_group("safety and diagnostics")
    safety.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Walk the entire flow but never click Save. Safe against a live account.",
    )
    safety.add_argument("--log-level", default=None,
                        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"])
    safety.add_argument("--json-logs", action="store_true", default=None,
                        help="Emit structured JSON log lines instead of human-readable ones")
    safety.add_argument("--print-config", action="store_true",
                        help="Resolve and print the effective configuration, then exit")
    safety.add_argument("--diagnose", metavar="PATH", nargs="?", const="diagnose.json",
                        help="Log in and dump what the live site renders (grid links, "
                             "modal contents, duration options) to a JSON file. Books nothing.")
    safety.add_argument("--ignore-ledger", action="store_true",
                        help="Ignore the already-booked guard and run anyway")

    return parser


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """Map CLI flags onto configuration keys, skipping anything not supplied."""
    mapping: dict[str, Any] = {
        "court_date": args.date,
        "days_in_advance": args.days_in_advance,
        "search_start_time": args.start_time,
        "search_end_time": args.end_time,
        "booking_priority": args.booking_priority,
        "release_time": args.release_time,
        "sprint_seconds": args.sprint_seconds,
        "headless": args.headless,
        "webdriver_timeout": args.webdriver_timeout,
        "dry_run": args.dry_run,
    }
    if args.durations:
        try:
            mapping["preferred_durations"] = [
                int(part.strip()) for part in args.durations.split(",") if part.strip()
            ]
        except ValueError as exc:
            raise ConfigurationError(
                f"--durations must be a comma-separated list of minutes, got {args.durations!r}"
            ) from exc
    return {key: value for key, value in mapping.items() if value is not None}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Before configure_logging: .env may set LOG_LEVEL / LOG_JSON.
    dotenv_path = load_dotenv_once()

    configure_logging(
        level=args.log_level or os.environ.get("LOG_LEVEL", "INFO"),
        json_output=bool(args.json_logs),
        log_dir=os.environ.get("LOG_DIR"),
        secrets=[os.environ.get("PASSWORD")],
    )
    if dotenv_path:
        logger.debug(f"Loaded environment from {dotenv_path}")

    try:
        settings = load_settings(args.config, overrides_from(args))
    except ConfigurationError as exc:
        logger.error(str(exc))
        return EXIT_CONFIG

    configure_logging(
        level=args.log_level or settings.runtime.log_level,
        json_output=bool(args.json_logs) if args.json_logs is not None
        else settings.runtime.log_json,
        log_dir=settings.runtime.log_dir,
        secrets=[settings.credentials.password],
    )

    if args.print_config:
        import json

        effective = settings.booking.model_dump(mode="json")
        effective["_resolved_court_date"] = settings.booking.target_date().strftime("%m/%d/%Y")
        effective["_account"] = settings.credentials.masked_username

        rotation = settings.booking.rotation
        if rotation is not None:
            from .timeutil import now_in  # noqa: PLC0415

            today = now_in(settings.booking.tz).date()
            effective["_next_play_dates"] = rotation.calendar(
                after=today,
                count=8,
                days_in_advance=settings.booking.days_in_advance,
            )
            effective["_today_is_a_play_date"] = rotation.is_play_date(today)

        # The window actually used on each upcoming date, after weekday
        # overrides. A wrong override is invisible in the raw config — it just
        # books the default window on that day forever — so it is spelled out.
        if settings.booking.weekday_overrides:
            from .timeutil import now_in  # noqa: PLC0415

            today = now_in(settings.booking.tz).date()
            # Same count as _next_play_dates, so the two lists read side by side.
            upcoming = (
                rotation.next_play_dates(after=today, count=8)
                if rotation is not None
                else [today]
            )
            effective["_effective_windows"] = [
                f"{day:%a %d %b}  "
                f"{resolved.search_start_time:%-I:%M %p}-{resolved.search_end_time:%-I:%M %p}  "
                f"tries {resolved.priority_time_slots or 'the whole window in order'}"
                for day in upcoming
                for resolved in [settings.booking.for_date(day)]
            ]
        print(json.dumps(effective, indent=2, sort_keys=True))
        return EXIT_BOOKED

    if args.diagnose:
        import json

        try:
            diagnosis = CourtBookingBot(settings).diagnose()
        except Exception as exc:  # noqa: BLE001 - report rather than traceback
            logger.error(f"Diagnostics failed: {type(exc).__name__}: {exc}")
            return EXIT_INFRA
        Path(args.diagnose).write_text(json.dumps(diagnosis, indent=2), encoding="utf-8")
        _summarise_diagnosis(diagnosis)
        logger.info(f"Full diagnostics written to {args.diagnose}")
        return EXIT_BOOKED

    kwargs: dict[str, Any] = {}
    if args.ignore_ledger:
        from .ledger import NullLedger

        kwargs["ledger"] = NullLedger()

    report = CourtBookingBot(settings, **kwargs).run()

    if report.skipped_reason:
        # Nothing to do today is a correct outcome, not a failure.
        logger.info(f"Skipped: {report.detail or report.skipped_reason}")
        return EXIT_BOOKED
    if report.error:
        logger.error(f"Run failed: {report.error}")
        return EXIT_INFRA
    if report.dry_run:
        logger.info(f"Dry run complete: {report.detail or 'nothing available'}")
        return EXIT_BOOKED
    return EXIT_BOOKED if report.booked else EXIT_NOT_BOOKED


def _summarise_diagnosis(report: dict[str, Any]) -> None:
    """Print the two answers that matter, so the JSON is usually unnecessary."""
    on_grid = [c["slot"] for c in report.get("candidates", []) if c["on_grid"]]
    logger.info(
        f"Grid has {report.get('grid_link_count', 0)} bookable link(s); "
        f"{len(on_grid)} of your candidates are present: {on_grid or 'none'}"
    )
    modal = report.get("modal") or {}
    if modal:
        offered = modal.get("duration_options_dom") or modal.get("duration_options_widget")
        expected = (report.get("config") or {}).get("duration_labels_expected")
        logger.info(f"Modal opened for {report.get('modal_opened_for')}")
        logger.info(f"Durations the site offers: {offered}")
        logger.info(f"Durations this config expects: {expected}")
        logger.info(
            "Buttons in the modal: "
            f"{[b['text'] for b in modal.get('buttons_in_modal', [])]}"
        )
    else:
        logger.warning("No slot would open a reservation modal — nothing bookable right now.")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
