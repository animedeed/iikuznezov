"""Calendar seams and value types. Nothing here performs I/O."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from pydantic import Field, model_validator

from app.contracts import Contract, Ref, ShortText, UtcMoment


class CalendarTemporaryError(Exception):
    """The provider refused the call in a way that is known to be safe to retry."""

    def __init__(self, code: str = "calendar_unavailable", retry_after_seconds: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class CalendarPermanentError(Exception):
    """The provider rejected the request; retrying cannot help."""

    def __init__(self, code: str = "calendar_rejected"):
        super().__init__(code)
        self.code = code


class CalendarConflictError(Exception):
    """The provider already holds this UID with different content (someone edited it).
    A person must decide; the workflow never overwrites it."""

    def __init__(self, code: str = "calendar_conflict"):
        super().__init__(code)
        self.code = code


class CalendarEvent(Contract):
    """An existing calendar entry, as read for a plan or a busy check."""

    id: Ref
    uid: Ref | None = None
    origin: Ref = "unknown"
    title: ShortText
    starts_at: UtcMoment
    ends_at: UtcMoment | None = None
    all_day: bool = False
    busy: bool = True


class EventSpec(Contract):
    """Immutable description of an event to write. Either all-day or timed."""

    uid: Ref
    summary: ShortText
    description: str = Field(default="", max_length=4_000)
    start_date: date | None = None
    starts_at: UtcMoment | None = None
    ends_at: UtcMoment | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "EventSpec":
        all_day = self.start_date is not None
        timed = self.starts_at is not None or self.ends_at is not None
        if all_day == timed:
            raise ValueError("Event must be either all-day or timed")
        if timed and (self.starts_at is None or self.ends_at is None or self.ends_at <= self.starts_at):
            raise ValueError("Timed event needs starts_at < ends_at")
        return self


class CalendarPort(Protocol):
    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        """Events overlapping [start, end). Raises on provider failure; never returns partial data."""
        ...

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        """Create the event or return the ID of an identical existing event."""
        ...
