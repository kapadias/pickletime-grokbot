"""Secrets must not reach the log stream."""

from __future__ import annotations

import io
import json

from loguru import logger

from pickletime.logging_setup import _make_sink, configure_logging, redact, register_secret


def capture(*, as_json: bool, level: str = "DEBUG") -> io.StringIO:
    stream = io.StringIO()
    logger.remove()
    logger.add(_make_sink(stream, as_json=as_json), level=level, backtrace=False, diagnose=False)
    return stream


def test_registered_secrets_are_scrubbed_from_messages() -> None:
    register_secret("correct-horse-battery")
    stream = capture(as_json=False)

    logger.info("Logging in with correct-horse-battery now")
    output = stream.getvalue()

    assert "correct-horse-battery" not in output
    assert "***REDACTED***" in output


def test_secrets_are_scrubbed_from_bound_context() -> None:
    register_secret("swordfish-1234")
    stream = capture(as_json=True)

    logger.bind(detail="password is swordfish-1234").info("bound context")
    payload = json.loads(stream.getvalue().strip())

    assert "swordfish-1234" not in json.dumps(payload)


def test_secrets_are_scrubbed_from_tracebacks() -> None:
    """An exception whose text embeds the password must not leak it."""
    register_secret("hunter22-secret")
    stream = capture(as_json=True)

    try:
        raise ValueError("login failed for user with password hunter22-secret")
    except ValueError:
        logger.exception("boom")

    output = stream.getvalue()
    assert "hunter22-secret" not in output
    assert "***REDACTED***" in output


def test_short_values_are_not_registered() -> None:
    """Redacting a 3-character secret would mangle every log line."""
    register_secret("abc")
    assert redact("abcdef") == "abcdef"


def test_json_output_uses_cloud_logging_severity() -> None:
    stream = capture(as_json=True)
    logger.warning("careful")
    payload = json.loads(stream.getvalue().strip())

    assert payload["severity"] == "WARNING"
    assert payload["message"] == "careful"
    assert "timestamp" in payload
    assert payload["logging.googleapis.com/sourceLocation"]["function"]


def test_success_maps_to_notice() -> None:
    stream = capture(as_json=True)
    logger.success("booked")
    assert json.loads(stream.getvalue().strip())["severity"] == "NOTICE"


def test_human_output_is_single_line_per_record() -> None:
    stream = capture(as_json=False)
    logger.info("one")
    logger.info("two")
    assert len(stream.getvalue().strip().splitlines()) == 2


def test_configure_logging_does_not_create_directories_by_default(tmp_path) -> None:
    """The old module created ./logs at import time, on every run and in tests."""
    configure_logging(level="INFO", json_output=True, log_dir=None)
    assert not (tmp_path / "logs").exists()


def test_configure_logging_writes_a_file_when_asked(tmp_path) -> None:
    directory = tmp_path / "logs"
    configure_logging(level="INFO", json_output=False, log_dir=str(directory))
    logger.info("to disk")
    assert directory.is_dir()
    assert any(directory.iterdir())
    configure_logging(level="WARNING", json_output=False)  # restore for other tests
