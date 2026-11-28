"""Busy checks for timed and all-day calendar occurrences."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.calendar.ports import CalendarEvent

OWN_UID_SUFFIX = "@iikuznezov"


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _end(event: CalendarEvent) -> datetime:
    return event.ends_at if event.ends_at and event.ends_at > event.starts_at else event.starts_at + timedelta(seconds=1)


def blocking_events(
    events: Sequence[CalendarEvent], start: datetime, end: datetime, *, ignore_uid: str | None = None,
) -> list[CalendarEvent]:
    return [
        e for e in events
        if e.busy and overlaps(e.starts_at, _end(e), start, end)
        and (e.uid or e.id) != ignore_uid
        and not (e.all_day and (e.uid or e.id).endswith(OWN_UID_SUFFIX))
    ]


def protected_overlap(
    protected: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime,
) -> bool:
    return any(overlaps(p_start, p_end, start, end) for p_start, p_end in protected)
