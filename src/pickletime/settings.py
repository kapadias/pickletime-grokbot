"""Validated configuration for the booking bot.

Three layers, lowest precedence first:

1. ``config.yaml`` (or ``--config``) — behaviour, checked into the repo.
2. Environment variables — secrets and per-deployment wiring only.
3. CLI / runner overrides — restricted to :data:`REQUEST_OVERRIDABLE_FIELDS`.

That third restriction is a security control, not tidiness: an override that
could set ``base_url`` would have the bot type the real account password into
whatever site it named. Credentials, URLs and filesystem paths are therefore
never overridable from a flag; they come only from config.yaml and the
environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, time
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigurationError
from .rotation import Rotation
from .timeutil import (
    format_clock_time,
    get_timezone,
    parse_clock_time,
    parse_us_date,
    resolve_court_date,
)

__all__ = [
    "BookingSettings",
    "Credentials",
    "DayPreferences",
    "RuntimeSettings",
    "Settings",
    "REQUEST_OVERRIDABLE_FIELDS",
    "Rotation",
    "load_dotenv_once",
    "load_settings",
    "duration_label",
]

DEFAULT_BASE_URL: Final = "https://app.courtreserve.com"

_dotenv_state: dict[str, Any] = {"loaded": False, "path": None}


def load_dotenv_once(path: str | Path | None = None) -> str | None:
    """Load a ``.env`` file into the environment. Returns the file used, if any.

    Real environment variables always win (``override=False``): a ``.env`` that
    somehow ends up in a container must never shadow a Secret Manager value.

    Set ``PICKLETIME_SKIP_DOTENV=1`` to disable — the test suite does this so a
    developer's real credentials cannot leak into a test run.
    """
    if _dotenv_state["loaded"]:
        return _dotenv_state["path"]
    _dotenv_state["loaded"] = True

    if os.environ.get("PICKLETIME_SKIP_DOTENV"):
        return None
    try:
        from dotenv import find_dotenv, load_dotenv  # noqa: PLC0415
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
        return None

    found = str(path) if path else find_dotenv(usecwd=True)
    if not found or not Path(found).is_file():
        return None

    load_dotenv(found, override=False)
    _dotenv_state["path"] = found
    return found

#: Fields a CLI flag or runner override is allowed to set.
#: Deliberately excludes every credential, URL, and filesystem path.
REQUEST_OVERRIDABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "booking_priority",
        "court_date",
        "days_in_advance",
        "dry_run",
        "headless",
        "max_retries",
        "preferred_durations",
        "priority_time_slots",
        "release_time",
        "retry_interval",
        "search_end_time",
        "search_start_time",
        "sport_id",
        "sprint_seconds",
        "time_increment",
        "timezone",
        "webdriver_timeout",
    }
)


def _dotenv_hint() -> str:
    """Say what happened with .env, so a missing variable is self-diagnosing."""
    if os.environ.get("PICKLETIME_SKIP_DOTENV"):
        return "(.env loading is disabled by PICKLETIME_SKIP_DOTENV)"
    loaded = _dotenv_state.get("path")
    if loaded:
        return f"Loaded {loaded}, but it did not define them."
    return f"No .env file was found (searched upwards from {Path.cwd()})."


def duration_label(minutes: int) -> str:
    """CourtReserve's dropdown text for a duration, e.g. ``90 -> '1 hour & 30 minutes'``."""
    hours, mins = divmod(minutes, 60)
    if hours == 0:
        return f"{mins} minutes"
    hour_part = "1 hour" if hours == 1 else f"{hours} hours"
    return hour_part if mins == 0 else f"{hour_part} & {mins} minutes"


class Credentials(BaseModel):
    """Booking-site login. Sourced from the environment only, never from a request."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(min_length=1)
    password: str = Field(min_length=1, repr=False)

    @property
    def masked_username(self) -> str:
        """``s***k@gmail.com`` — enough to confirm which account ran, not enough to harvest."""
        local, _, domain = self.username.partition("@")
        if not domain:
            return f"{local[:1]}***"
        head = local[:1] if local else ""
        tail = local[-1:] if len(local) > 1 else ""
        return f"{head}***{tail}@{domain}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"Credentials({self.masked_username})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Credentials:
        if env is None:
            load_dotenv_once()
        source = os.environ if env is None else env
        username = (source.get("USERID") or source.get("PICKLETIME_USERNAME") or "").strip()
        password = source.get("PASSWORD") or source.get("PICKLETIME_PASSWORD") or ""
        missing = [
            name
            for name, value in (("USERID", username), ("PASSWORD", password))
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Missing credential environment variable(s): {', '.join(missing)}.\n"
                f"{_dotenv_hint()}\n"
                "Fix it by creating a .env file in the project root:\n"
                "    USERID=your_email@example.com\n"
                "    PASSWORD=your_password\n"
                "or by exporting them:  export USERID=... PASSWORD=...\n"
                "On Grok Bot, add them with the secure secret card (SETUP.md section 4)."
            )
        return cls(username=username, password=password)


WEEKDAYS: Final[tuple[str, ...]] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


class DayPreferences(BaseModel):
    """Times that replace the defaults on one weekday.

    Separate from :class:`Rotation` because the two answer different questions.
    The rotation decides *which dates* are free, and repeats on a 14-day cycle
    that has nothing to do with weekdays. When you play on a given day is a
    property of the day itself — Saturday mornings are free whichever half of
    the cycle the Saturday falls in — so it keys on the weekday name.

    Every field is optional; an unset one keeps the top-level value.
    """

    model_config = ConfigDict(extra="forbid")

    search_start_time: time | None = None
    search_end_time: time | None = None
    priority_time_slots: list[str] | None = None
    preferred_durations: list[int] | None = None

    @field_validator("search_start_time", "search_end_time", mode="before")
    @classmethod
    def _coerce_clock(cls, value: Any) -> Any:
        return parse_clock_time(value) if isinstance(value, str) else value

    def changes(self) -> dict[str, Any]:
        """The fields actually set, ready to merge over the defaults."""
        return self.model_dump(exclude_none=True)


class BookingSettings(BaseModel):
    """Everything that decides *what* and *when* to book."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ---- facility / site ------------------------------------------------
    base_url: str = DEFAULT_BASE_URL
    organization_id: int = 13233
    sport_id: int = 16984
    sport_name: str = "Pickleball"
    court_type: str = "Pickleball"
    #: Appears in the grid's slot URLs as `courtTypeId`. Optional: matching falls
    #: back to the start time + scheduler id, which does not need it.
    court_type_id: int | None = None

    # ---- what to book ---------------------------------------------------
    timezone: str = "America/Los_Angeles"
    days_in_advance: int = Field(default=8, ge=0, le=365)
    court_date: date | None = None
    search_start_time: time = Field(default_factory=lambda: parse_clock_time("10:00 AM"))
    search_end_time: time = Field(default_factory=lambda: parse_clock_time("12:00 PM"))
    time_increment: int = Field(default=30, ge=5, le=240)
    slot_length_minutes: int = Field(default=30, ge=5, le=240)
    preferred_durations: list[int] = Field(default_factory=lambda: [60, 30])
    booking_priority: Literal["time", "duration"] = "duration"
    priority_time_slots: list[str] = Field(default_factory=list)
    duration_labels: dict[int, str] = Field(default_factory=dict)
    #: Only book court dates that fall on this repeating pattern. Unset = every day.
    rotation: Rotation | None = None
    #: Per-weekday time preferences, e.g. mornings on Saturday but evenings the
    #: rest of the week. Keyed by weekday name; anything absent uses the values
    #: above. Resolved by :meth:`for_date` once the court date is known.
    weekday_overrides: dict[str, DayPreferences] = Field(default_factory=dict)

    # ---- race strategy --------------------------------------------------
    release_time: time | None = None
    sprint_seconds: int = Field(default=25, ge=0, le=600)
    #: Guard rail: never park a request longer than this waiting for release_time.
    max_arm_wait_seconds: int = Field(default=300, ge=0, le=1800)
    #: How long past release_time to keep trying for a date that has not appeared
    #: yet. A club's "noon" is its server's noon, and can run a minute or two
    #: late; giving up early forfeits the booking for a clock difference.
    release_grace_seconds: int = Field(default=180, ge=0, le=900)
    poll_interval_ms: int = Field(default=150, ge=20, le=5_000)
    max_retries: int = Field(default=1, ge=0, le=10)
    retry_interval: int = Field(default=5, ge=0, le=600)

    # ---- browser --------------------------------------------------------
    headless: bool = True
    webdriver_timeout: int = Field(default=15, ge=1, le=300)
    page_load_timeout: int = Field(default=30, ge=1, le=300)
    chrome_binary: str | None = None
    chrome_driver_path: str | None = None
    window_size: str = "1920,1080"
    block_images: bool = True

    # ---- safety / observability ----------------------------------------
    dry_run: bool = False
    screenshot_dir: str | None = None

    # -- coercion ---------------------------------------------------------
    @field_validator("search_start_time", "search_end_time", "release_time", mode="before")
    @classmethod
    def _coerce_clock(cls, value: Any) -> Any:
        return parse_clock_time(value) if isinstance(value, str) else value

    @field_validator("court_date", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return parse_us_date(value)
            except ValueError as exc:
                raise ValueError("court_date must be in MM/DD/YYYY format") from exc
        return value

    @field_validator("priority_time_slots", mode="before")
    @classmethod
    def _normalise_priority_slots(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        # Normalise so "07:30 PM" in YAML still matches the "7:30 PM" labels we build.
        return [format_clock_time(parse_clock_time(str(item))) for item in value]

    @field_validator("weekday_overrides", mode="before")
    @classmethod
    def _normalise_weekdays(cls, value: Any) -> Any:
        """Accept any casing, reject anything that is not a weekday.

        A typo here would otherwise be silent: the override simply never
        matches, and the bot books the default window on that day forever.
        """
        if not isinstance(value, dict):
            return value
        canonical = {day.lower(): day for day in WEEKDAYS}
        normalised: dict[str, Any] = {}
        for key, prefs in value.items():
            name = canonical.get(str(key).strip().lower())
            if name is None:
                raise ValueError(
                    f"weekday_overrides key {key!r} is not a weekday name; "
                    f"expected one of {', '.join(WEEKDAYS)}"
                )
            normalised[name] = prefs
        return normalised

    @field_validator("preferred_durations")
    @classmethod
    def _check_durations(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("preferred_durations must list at least one duration")
        for minutes in value:
            if minutes <= 0 or minutes % 5 != 0 or minutes > 480:
                raise ValueError(
                    f"Invalid duration {minutes!r}: use whole multiples of 5 minutes, up to 480"
                )
        deduped = list(dict.fromkeys(value))  # order-preserving, drops accidental repeats
        return deduped

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        # The account password is typed into whatever this points at, so plain
        # http is only tolerated against a loopback address (the test fixture).
        cleaned = value.strip().rstrip("/")
        if cleaned.startswith("https://"):
            return cleaned
        if cleaned.startswith("http://"):
            host = cleaned[len("http://") :].split("/", 1)[0].split(":", 1)[0]
            if host in {"localhost", "127.0.0.1", "[::1]", "::1"}:
                return cleaned
        raise ValueError(
            "base_url must be an https:// URL — credentials are typed into it "
            "(plain http is allowed only for localhost)"
        )

    @field_validator("window_size")
    @classmethod
    def _check_window_size(cls, value: str) -> str:
        parts = value.split(",")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            raise ValueError("window_size must look like '1920,1080'")
        return value

    # -- cross-field checks ----------------------------------------------
    @model_validator(mode="after")
    def _check_consistency(self) -> BookingSettings:
        if self.search_end_time < self.search_start_time:
            raise ValueError(
                "search_end_time must not be earlier than search_start_time "
                f"({format_clock_time(self.search_end_time)} < "
                f"{format_clock_time(self.search_start_time)})"
            )
        get_timezone(self.timezone)  # fail fast on a bad zone name
        return self

    # -- derived ----------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return get_timezone(self.timezone)

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/Online/Account/LogIn/{self.organization_id}"

    @property
    def bookings_path(self) -> str:
        return f"/Online/Reservations/Bookings/{self.organization_id}?sId={self.sport_id}"

    @property
    def bookings_url(self) -> str:
        return f"{self.base_url}{self.bookings_path}"

    def label_for_duration(self, minutes: int) -> str:
        return self.duration_labels.get(minutes) or duration_label(minutes)

    def target_date(self, *, now: Any = None) -> date:
        """The date to book — explicit ``court_date`` wins, else days-in-advance."""
        if self.court_date is not None:
            return self.court_date
        return resolve_court_date(self.days_in_advance, self.tz, now=now)

    def for_date(self, day: date) -> BookingSettings:
        """These settings with any ``weekday_overrides`` for ``day`` applied.

        Re-validated rather than copied field-by-field, so an override that
        produces an impossible combination — an end time before its start, say —
        fails here instead of silently searching an empty window.
        """
        override = self.weekday_overrides.get(day.strftime("%A"))
        if override is None:
            return self
        changes = override.changes()
        if not changes:
            return self
        return BookingSettings.model_validate({**self.model_dump(), **changes})


class RuntimeSettings(BaseModel):
    """Process-level wiring: logging and state. Environment-driven."""

    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    log_json: bool = False
    log_dir: str | None = None
    # scripts/env.sh points this into /workspace, which survives the Grok
    # computer's sleeps; /tmp is only the fallback for ad-hoc runs.
    ledger_path: str = "/tmp/pickletime-ledger.json"  # noqa: S108
    ledger_ttl_hours: int = Field(default=36, ge=0, le=24 * 30)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeSettings:
        if env is None:
            load_dotenv_once()
        source = os.environ if env is None else env
        return cls(
            log_level=source.get("LOG_LEVEL", "INFO"),
            log_json=_env_bool(source, "LOG_JSON", default=False),
            log_dir=source.get("LOG_DIR") or None,
            ledger_path=source.get("PICKLETIME_LEDGER_PATH", "/tmp/pickletime-ledger.json"),  # noqa: S108
            ledger_ttl_hours=int(source.get("PICKLETIME_LEDGER_TTL_HOURS", "36")),
        )


class Settings(BaseModel):
    """The fully-resolved configuration handed to the bot."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    booking: BookingSettings
    credentials: Credentials
    runtime: RuntimeSettings


def _env_bool(source: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = source.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file not found: {config_path}")
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{config_path} must contain a YAML mapping at the top level")
    return loaded


def _env_overrides(source: Mapping[str, str]) -> dict[str, Any]:
    """Deployment wiring that belongs in the environment, not in config.yaml."""
    overrides: dict[str, Any] = {}
    if driver := source.get("CHROME_DRIVER_PATH"):
        overrides["chrome_driver_path"] = driver
    if binary := source.get("CHROME_BIN"):
        overrides["chrome_binary"] = binary
    if "HEADLESS" in source:
        overrides["headless"] = _env_bool(source, "HEADLESS", default=True)
    if "PICKLETIME_DRY_RUN" in source:
        overrides["dry_run"] = _env_bool(source, "PICKLETIME_DRY_RUN", default=False)
    if shots := source.get("PICKLETIME_SCREENSHOT_DIR"):
        overrides["screenshot_dir"] = shots
    return overrides


def load_settings(
    config_file: str | Path = "config.yaml",
    overrides: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    require_credentials: bool = True,
) -> Settings:
    """Build a validated :class:`Settings` from YAML + environment + overrides."""
    if env is None:
        load_dotenv_once()
    source = os.environ if env is None else env

    data: dict[str, Any] = _read_yaml(config_file)
    data.update(_env_overrides(source))
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})

    # Accept the legacy key name so an old config.yaml keeps working.
    if "chromedriver_path" in data and "chrome_driver_path" not in data:
        data["chrome_driver_path"] = data.pop("chromedriver_path")

    try:
        booking = BookingSettings(**data)
    except ConfigurationError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigurationError(f"Invalid configuration in {config_file}: {exc}") from exc

    if require_credentials:
        credentials = Credentials.from_env(source)
    else:
        credentials = Credentials(username="unset@example.com", password="unset")  # noqa: S106

    return Settings(
        booking=booking,
        credentials=credentials,
        runtime=RuntimeSettings.from_env(source),
    )
