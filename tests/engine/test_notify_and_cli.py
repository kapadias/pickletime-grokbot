"""Notifications and the command-line surface."""

from __future__ import annotations

from pathlib import Path

import pytest

import pickletime.cli as cli_module
from pickletime.bot import RunReport
from pickletime.cli import (
    EXIT_BOOKED,
    EXIT_CONFIG,
    EXIT_INFRA,
    EXIT_NOT_BOOKED,
    build_parser,
    overrides_from,
)
from pickletime.cli import main as cli_main
from pickletime.errors import ConfigurationError
from pickletime.notify import NullNotifier, build_notifier
from pickletime.settings import BookingSettings

CONFIG = """
organization_id: 13233
sport_id: 16984
days_in_advance: 8
search_start_time: "10:00 AM"
search_end_time: "12:00 PM"
preferred_durations: [60, 30]
"""


# -- notifications ----------------------------------------------------------
def test_build_notifier_is_null() -> None:
    assert isinstance(build_notifier(BookingSettings()), NullNotifier)


def test_null_notifier_never_raises() -> None:
    notifier = NullNotifier()
    notifier.success({"date": "7/22/2025", "time": "11:30 AM"})
    notifier.failure("nothing available")
    notifier.error("chrome died")


# -- CLI --------------------------------------------------------------------
def test_documented_flags_still_parse() -> None:
    """Every documented flag keeps working."""
    args = build_parser().parse_args(
        [
            "--config", "my.yaml", "--date", "12/25/2025", "--days-in-advance", "7",
            "--start-time", "6:00 PM", "--end-time", "9:00 PM", "--durations", "60,30",
            "--headless",
        ]
    )
    assert args.config == "my.yaml"
    assert args.date == "12/25/2025"
    assert args.headless is True


def test_overrides_skip_unsupplied_flags() -> None:
    args = build_parser().parse_args(["--date", "12/25/2025"])
    assert overrides_from(args) == {"court_date": "12/25/2025"}


def test_durations_are_parsed_into_ints() -> None:
    args = build_parser().parse_args(["--durations", " 90 , 60,30 "])
    assert overrides_from(args)["preferred_durations"] == [90, 60, 30]


def test_bad_durations_raise_a_configuration_error() -> None:
    args = build_parser().parse_args(["--durations", "sixty"])
    with pytest.raises(ConfigurationError, match="comma-separated"):
        overrides_from(args)


def test_no_headless_flag_disables_headless() -> None:
    args = build_parser().parse_args(["--no-headless"])
    assert overrides_from(args)["headless"] is False


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setenv("USERID", "player@example.com")
    monkeypatch.setenv("PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("PICKLETIME_LEDGER_PATH", str(tmp_path / "ledger.json"))
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return str(path)


def _stub_bot(monkeypatch, report: RunReport) -> list[dict]:
    seen: list[dict] = []

    class Stub:
        def __init__(self, settings, **kwargs):
            seen.append(kwargs)

        def run(self) -> RunReport:
            return report

    monkeypatch.setattr(cli_module, "CourtBookingBot", Stub)
    return seen


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (RunReport(run_id="r", booked=True, court_date="7/22/2025"), EXIT_BOOKED),
        (RunReport(run_id="r", booked=False, court_date="7/22/2025"), EXIT_NOT_BOOKED),
        (RunReport(run_id="r", booked=False, court_date="7/22/2025", error="boom"), EXIT_INFRA),
        (RunReport(run_id="r", booked=False, court_date="7/22/2025", dry_run=True), EXIT_BOOKED),
        # A day off the rotation is a correct outcome, not "nothing available"
        # and not a failure — the scheduler must not see it as either.
        (
            RunReport(
                run_id="r", booked=False, court_date="7/22/2025",
                skipped_reason="outside the rotation",
            ),
            EXIT_BOOKED,
        ),
    ],
)
def test_exit_codes_are_meaningful(monkeypatch, config_path, report, expected) -> None:
    _stub_bot(monkeypatch, report)
    assert cli_main(["--config", config_path]) == expected


def test_missing_config_exits_with_the_config_code(tmp_path) -> None:
    assert cli_main(["--config", str(tmp_path / "absent.yaml")]) == EXIT_CONFIG


def test_print_config_shows_the_resolved_values(monkeypatch, config_path, capsys) -> None:
    assert cli_main(["--config", config_path, "--print-config"]) == EXIT_BOOKED
    printed = capsys.readouterr().out

    assert '"_resolved_court_date"' in printed
    assert "p***r@example.com" in printed
    assert "correct-horse-battery" not in printed


def test_ignore_ledger_passes_a_null_ledger(monkeypatch, config_path) -> None:
    from pickletime.ledger import NullLedger

    seen = _stub_bot(monkeypatch, RunReport(run_id="r", booked=True, court_date="7/22/2025"))
    cli_main(["--config", config_path, "--ignore-ledger"])
    assert isinstance(seen[0]["ledger"], NullLedger)
