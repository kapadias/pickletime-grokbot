"""`.env` loading.

This existed implicitly in the original code (``password_manager`` called
``load_dotenv()`` at import) and was lost in the rewrite: ``python-dotenv``
stayed a dependency while nothing called it, so a correctly-written ``.env`` was
silently ignored and every local run failed with "Missing credential environment
variable(s)". These tests pin the behaviour down.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pickletime import settings as settings_module
from pickletime.errors import ConfigurationError
from pickletime.settings import Credentials, load_dotenv_once, load_settings

CONFIG = """
organization_id: 13233
sport_id: 16984
days_in_advance: 8
"""


@pytest.fixture
def fresh_dotenv(monkeypatch):
    """Reset the once-only guard and allow .env loading for this test."""
    monkeypatch.setattr(settings_module, "_dotenv_state", {"loaded": False, "path": None})
    monkeypatch.delenv("PICKLETIME_SKIP_DOTENV", raising=False)
    for name in ("USERID", "PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_credentials_are_read_from_a_dotenv_file(tmp_path, fresh_dotenv) -> None:
    write_env(tmp_path, "USERID=player@example.com\nPASSWORD=correct-horse\n")
    load_dotenv_once(tmp_path / ".env")

    creds = Credentials.from_env()
    assert creds.username == "player@example.com"
    assert creds.password == "correct-horse"


def test_dotenv_is_found_by_walking_up_from_the_working_directory(
    tmp_path, fresh_dotenv, monkeypatch
) -> None:
    """Running from a subdirectory must still find the project's .env."""
    write_env(tmp_path, "USERID=player@example.com\nPASSWORD=correct-horse\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert load_dotenv_once() is not None
    assert os.environ["USERID"] == "player@example.com"


def test_real_environment_wins_over_dotenv(tmp_path, fresh_dotenv, monkeypatch) -> None:
    """A stray .env must never shadow a Secret Manager value."""
    monkeypatch.setenv("PASSWORD", "from-secret-manager")
    write_env(tmp_path, "USERID=player@example.com\nPASSWORD=from-dotenv\n")
    load_dotenv_once(tmp_path / ".env")

    assert Credentials.from_env().password == "from-secret-manager"


def test_values_with_spaces_and_hashes_survive(tmp_path, fresh_dotenv) -> None:
    """A naive `export $(... | xargs)` mangles exactly these."""
    write_env(tmp_path, 'USERID=player@example.com\nPASSWORD="p@ss w#rd \'quoted\'"\n')
    load_dotenv_once(tmp_path / ".env")

    assert Credentials.from_env().password == "p@ss w#rd 'quoted'"


def test_dotenv_can_supply_non_credential_settings(tmp_path, fresh_dotenv) -> None:
    write_env(tmp_path, "USERID=a@b.com\nPASSWORD=secret-value\nHEADLESS=false\nLOG_LEVEL=DEBUG\n")
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    load_dotenv_once(tmp_path / ".env")

    resolved = load_settings(tmp_path / "config.yaml")
    assert resolved.booking.headless is False
    assert resolved.runtime.log_level == "DEBUG"


def test_loading_is_idempotent(tmp_path, fresh_dotenv) -> None:
    write_env(tmp_path, "USERID=a@b.com\nPASSWORD=secret-value\n")
    assert load_dotenv_once(tmp_path / ".env") is not None
    assert load_dotenv_once(tmp_path / ".env") is not None  # cached, not reloaded


def test_skip_flag_disables_loading(tmp_path, fresh_dotenv, monkeypatch) -> None:
    monkeypatch.setenv("PICKLETIME_SKIP_DOTENV", "1")
    write_env(tmp_path, "USERID=a@b.com\nPASSWORD=secret-value\n")

    assert load_dotenv_once(tmp_path / ".env") is None
    assert "USERID" not in os.environ


def test_error_says_no_dotenv_was_found(tmp_path, fresh_dotenv, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match="No .env file was found"):
        Credentials.from_env()


def test_error_says_the_dotenv_was_found_but_incomplete(tmp_path, fresh_dotenv) -> None:
    """Distinguishing these two is the difference between a 10-second fix and an hour."""
    load_dotenv_once(write_env(tmp_path, "SOMETHING_ELSE=1\n"))
    with pytest.raises(ConfigurationError, match="but it did not define them"):
        Credentials.from_env()


def test_no_dotenv_present_is_not_an_error(tmp_path, fresh_dotenv, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_dotenv_once() is None
