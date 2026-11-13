"""Structured logging with secret redaction.

Cloud Run reads structured JSON on stdout, so that is the default sink: no log
files inside the container (they only ate tmpfs and vanished with the instance)
and no ``logs/`` directory to create at import time.

Every registered secret is scrubbed from message text at the sink, which is the
last point before bytes leave the process.  That covers the accidental cases —
an exception whose ``str()`` embeds a URL with the password, a page-source dump
during debugging — not just the places we remembered to be careful.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC
from pathlib import Path
from types import FrameType
from typing import Any

from loguru import logger

__all__ = ["configure_logging", "register_secret", "redact", "get_logger"]

# Loguru level -> Cloud Logging severity. Anything unmapped passes through.
_SEVERITY: dict[str, str] = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "NOTICE",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}

_REDACTION = "***REDACTED***"
_secrets: set[str] = set()
_configured = False


def register_secret(*values: str | None) -> None:
    """Mark values that must never appear in log output.

    Short values are ignored: redacting a 3-character password would replace
    every incidental occurrence of those characters and make logs unreadable.
    """
    for value in values:
        if value and len(value) >= 4:
            _secrets.add(value)


def redact(text: str) -> str:
    """Replace every registered secret in ``text``."""
    if not _secrets:
        return text
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, _REDACTION)
    return text


def _json_default(value: Any) -> str:
    return str(value)


def _make_sink(stream: Any, *, as_json: bool) -> Any:
    def sink(message: Any) -> None:
        record = message.record
        text = redact(record["message"])

        if not as_json:
            time_str = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            line = (
                f"{time_str} | {record['level'].name:<8} | "
                f"{record['name']}:{record['function']}:{record['line']} - {text}"
            )
            if record["exception"] is not None:
                line += "\n" + redact(_format_exception(record["exception"]))
            stream.write(line + "\n")
            stream.flush()
            return

        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record["level"].name, record["level"].name),
            "message": text,
            "timestamp": record["time"].astimezone(UTC).isoformat(),
            "logging.googleapis.com/sourceLocation": {
                "file": record["file"].path,
                "line": record["line"],
                "function": record["function"],
            },
        }
        extra = {k: v for k, v in record["extra"].items() if not k.startswith("_")}
        if extra:
            payload["context"] = json.loads(
                redact(json.dumps(extra, default=_json_default))
            )
        if record["exception"] is not None:
            payload["exception"] = redact(_format_exception(record["exception"]))

        stream.write(json.dumps(payload, default=_json_default) + "\n")
        stream.flush()

    return sink


def _format_exception(exception: Any) -> str:
    import traceback

    return "".join(
        traceback.format_exception(exception.type, exception.value, exception.traceback)
    ).rstrip()


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (selenium, urllib3, google-cloud) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin adapter
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk out of the logging machinery so loguru attributes the record to
        # the caller's file and line, not to logging/__init__.py.
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    log_dir: str | None = None,
    secrets: Iterable[str | None] = (),
    force: bool = True,
) -> None:
    """Install the logging configuration. Safe to call more than once."""
    global _configured

    register_secret(*secrets)

    if _configured and not force:
        return

    logger.remove()
    logger.add(
        _make_sink(sys.stdout, as_json=json_output),
        level=level,
        backtrace=False,
        diagnose=False,  # never render local variables: they hold the password
        enqueue=False,
    )

    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(directory / "pickletime_{time:YYYY-MM-DD}.log"),
            level=level,
            rotation="1 day",
            retention="7 days",
            backtrace=False,
            diagnose=False,
            format=lambda record: "{time} | {level} | " + redact(record["message"]) + "\n",
        )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("selenium", "urllib3", "werkzeug", "google", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(**context: Any) -> Any:
    """A logger bound to ``context`` (run id, court date, ...)."""
    return logger.bind(**context) if context else logger
