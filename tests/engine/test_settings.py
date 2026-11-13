"""Configuration validation and the request-override allowlist."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest

from pickletime.errors import ConfigurationError
from pickletime.settings import (
    REQUEST_OVERRIDABLE_FIELDS,
    BookingSettings,
    Credentials,
    RuntimeSettings,
    duration_label,
    load_settings,
)

MINIMAL_YAML = """
organization_id: 13233
sport_id: 16984
timezone: "America/Los_Angeles"
days_in_advance: 8
search_start_time: "10:00 AM"
search_end_time: "12:00 PM"
preferred_durations: [60, 30]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return path


ENV = {"USERID": "player@example.com", "PASSWORD": "correct-horse-battery"}


# -- validation -------------------------------------------------------------
def test_unknown_keys_are_rejected_loudly() -> None:
    """A typo in config.yaml used to be silently ignored — the model was dead code."""
    with pytest.raises(Exception, match="preffered_durations|extra"):
        BookingSettings(preffered_durations=[60])  # type: ignore[call-arg]


def test_reversed_search_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="search_end_time"):
        BookingSettings(search_start_time="9:00 PM", search_end_time="6:00 PM")


def test_bad_timezone_is_rejected() -> None:
    with pytest.raises((ValueError, ConfigurationError)):
        BookingSettings(timezone="Mars/Olympus_Mons")


def test_bad_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="multiples of 5"):
        BookingSettings(preferred_durations=[47])


def test_empty_durations_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one duration"):
        BookingSettings(preferred_durations=[])


def test_duplicate_durations_are_collapsed() -> None:
    assert BookingSettings(preferred_durations=[60, 30, 60]).preferred_durations == [60, 30]



def test_plain_http_base_url_is_rejected() -> None:
    """Credentials get typed into base_url, so it must be https."""
    with pytest.raises(ValueError, match="https"):
        BookingSettings(base_url="http://app.courtreserve.com")


def test_localhost_http_is_allowed_for_the_test_fixture() -> None:
    assert BookingSettings(base_url="http://127.0.0.1:8931").base_url == "http://127.0.0.1:8931"


def test_bad_court_date_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="MM/DD/YYYY"):
        BookingSettings(court_date="2025-12-25")


def test_priority_slots_are_normalised_to_the_sites_format() -> None:
    """'07:30 PM' in YAML must still match the '7:30 PM' labels we generate."""
    settings = BookingSettings(
        search_start_time="6:00 PM",
        search_end_time="9:00 PM",
        priority_time_slots=["07:30 PM", "6:00 pm"],
    )
    assert settings.priority_time_slots == ["7:30 PM", "6:00 PM"]


# -- derived values ---------------------------------------------------------
def test_urls_are_built_from_ids() -> None:
    settings = BookingSettings(organization_id=42, sport_id=7)
    assert settings.login_url == "https://app.courtreserve.com/Online/Account/LogIn/42"
    assert settings.bookings_url.endswith("/Online/Reservations/Bookings/42?sId=7")


def test_explicit_court_date_beats_days_in_advance() -> None:
    settings = BookingSettings(court_date="12/25/2025", days_in_advance=8)
    assert settings.target_date() == date(2025, 12, 25)


@pytest.mark.parametrize(
    ("minutes", "label"),
    [(30, "30 minutes"), (60, "1 hour"), (90, "1 hour & 30 minutes"), (120, "2 hours")],
)
def test_duration_labels_match_the_dropdown(minutes: int, label: str) -> None:
    assert duration_label(minutes) == label
    assert BookingSettings().label_for_duration(minutes) == label


def test_duration_labels_can_be_overridden() -> None:
    settings = BookingSettings(duration_labels={60: "60 min"})
    assert settings.label_for_duration(60) == "60 min"
    assert settings.label_for_duration(30) == "30 minutes"


# -- credentials ------------------------------------------------------------
def test_credentials_come_from_the_environment() -> None:
    creds = Credentials.from_env(ENV)
    assert creds.username == "player@example.com"
    assert creds.masked_username == "p***r@example.com"


def test_missing_credentials_name_the_variables() -> None:
    with pytest.raises(ConfigurationError, match="PASSWORD"):
        Credentials.from_env({"USERID": "a@b.com"})


def test_password_is_not_in_the_repr() -> None:
    creds = Credentials(username="a@b.com", password="super-secret")
    assert "super-secret" not in repr(creds)
    assert "super-secret" not in str(creds)


# -- the override allowlist (a security control) ----------------------------
@pytest.mark.parametrize(
    "field",
    ["base_url", "username", "password", "chrome_driver_path", "chrome_binary",
     "organization_id", "screenshot_dir"],
)
def test_sensitive_fields_are_not_request_overridable(field: str) -> None:
    """The old handler merged request JSON straight into config.

    Setting ``base_url`` from a request pointed the bot at an attacker's page and
    had it type the real account password into it.
    """
    assert field not in REQUEST_OVERRIDABLE_FIELDS


# -- loading ----------------------------------------------------------------
def test_load_settings_merges_yaml_env_and_overrides(config_file: Path) -> None:
    env = {**ENV, "CHROME_DRIVER_PATH": "/usr/local/bin/chromedriver", "HEADLESS": "false"}
    settings = load_settings(config_file, {"days_in_advance": 3}, env=env)

    assert settings.booking.days_in_advance == 3  # override wins
    assert settings.booking.chrome_driver_path == "/usr/local/bin/chromedriver"  # env
    assert settings.booking.headless is False
    assert settings.booking.search_start_time == time(10, 0)  # yaml
    assert settings.credentials.username == "player@example.com"


def test_load_settings_reports_a_missing_file_clearly(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_settings(tmp_path / "nope.yaml", env=ENV)


def test_load_settings_reports_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("days_in_advance: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="valid YAML"):
        load_settings(bad, env=ENV)


def test_load_settings_wraps_validation_errors(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("days_in_advance: -5\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid configuration"):
        load_settings(bad, env=ENV)


def test_legacy_chromedriver_path_key_still_works(tmp_path: Path) -> None:
    legacy = tmp_path / "config.yaml"
    legacy.write_text("chromedriver_path: /opt/chromedriver\n", encoding="utf-8")
    settings = load_settings(legacy, env=ENV)
    assert settings.booking.chrome_driver_path == "/opt/chromedriver"


# -- runtime ----------------------------------------------------------------
def test_json_logging_is_off_unless_asked_for() -> None:
    assert RuntimeSettings.from_env({}).log_json is False
    assert RuntimeSettings.from_env({"LOG_JSON": "true"}).log_json is True


def test_bad_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="log_level"):
        RuntimeSettings(log_level="CHATTY")
