"""Which court dates are worth booking.

A childcare rotation repeats every 14 days, and cron is inherently weekly, so
the schedule cannot express "book only on my free evenings". Instead the job
fires daily and this decides whether the resolved court date is one to act on —
before Chrome starts, so a skipped day costs nothing.

The cycle is a plain offset from an anchor date, which keeps it independent of
weekday arithmetic and of how any particular custody pattern is described.
Use :meth:`Rotation.calendar` to eyeball the result: a rotation that is off by a
day is not otherwise obvious until a court is booked on the wrong evening.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .timeutil import parse_us_date

__all__ = ["Rotation"]


class Rotation(BaseModel):
    """A repeating pattern of dates on which booking should be attempted."""

    model_config = ConfigDict(extra="forbid")

    #: Day 0 of the cycle. Any date works as long as the offsets are relative to it.
    anchor_date: date
    cycle_days: int = Field(default=14, ge=1, le=366)
    #: Offsets within the cycle to book on, e.g. ``[3, 7, 11]``.
    play_on_offsets: list[int] = Field(min_length=1)

    @field_validator("anchor_date", mode="before")
    @classmethod
    def _coerce_date(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return parse_us_date(value)
            except ValueError as exc:
                raise ValueError("anchor_date must be in MM/DD/YYYY format") from exc
        return value

    @model_validator(mode="after")
    def _check_offsets(self) -> Rotation:
        for offset in self.play_on_offsets:
            if not 0 <= offset < self.cycle_days:
                raise ValueError(
                    f"play_on_offsets must be within 0..{self.cycle_days - 1}, got {offset}"
                )
        if len(set(self.play_on_offsets)) != len(self.play_on_offsets):
            raise ValueError("play_on_offsets contains duplicates")
        return self

    # -- queries ----------------------------------------------------------
    def offset_of(self, day: date) -> int:
        """Where ``day`` falls in the cycle. Works for dates before the anchor."""
        return (day - self.anchor_date).days % self.cycle_days

    def is_play_date(self, day: date) -> bool:
        return self.offset_of(day) in set(self.play_on_offsets)

    def next_play_dates(self, *, after: date, count: int = 6) -> list[date]:
        """The next ``count`` bookable dates, starting from ``after`` inclusive."""
        found: list[date] = []
        day = after
        # One full cycle past the last possible hit is enough to collect `count`,
        # since at least one offset is guaranteed by the min_length validator.
        limit = after + timedelta(days=self.cycle_days * (count + 1))
        while day <= limit and len(found) < count:
            if self.is_play_date(day):
                found.append(day)
            day += timedelta(days=1)
        return found

    def calendar(self, *, after: date, count: int = 6, days_in_advance: int = 0) -> list[str]:
        """Human-checkable lines: when to fire, and what gets booked."""
        lines = []
        for day in self.next_play_dates(after=after, count=count):
            fires = day - timedelta(days=days_in_advance)
            lines.append(
                f"play {day:%a %d %b %Y}  (cycle day {self.offset_of(day):2})"
                + (f"   ← fires {fires:%a %d %b}" if days_in_advance else "")
            )
        return lines
