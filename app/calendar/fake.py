"""In-memory calendar for tests and offline demos."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta

from app.calendar.ports import CalendarConflictError, CalendarEvent, EventSpec


class FakeCalendar:
    def __init__(self, events: dict[str, Sequence[CalendarEvent]] | None = None):
        self.events: dict[str, list[CalendarEvent]] = {k: list(v) for k, v in (events or {}).items()}
        self.written: dict[tuple[str, str], EventSpec] = {}
        self.upsert_calls = 0
        self.failures: deque[Exception] = deque()

    def fail_next(self, error: Exception) -> None:
        self.failures.append(error)

    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        if self.failures:
            raise self.failures.popleft()
        return [
            e for e in self.events.get(profile_id, ())
            if e.starts_at < end and (e.ends_at or e.starts_at) >= start
        ]

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        self.upsert_calls += 1
        if self.failures:
            raise self.failures.popleft()
        if event.start_date is not None:
            starts = datetime.combine(event.start_date, time.min, tzinfo=UTC)
            read = CalendarEvent(id=event.uid, uid=event.uid, origin="fake", title=event.summary,
                                 starts_at=starts, ends_at=starts + timedelta(days=1), all_day=True)
        else:
            read = CalendarEvent(id=event.uid, uid=event.uid, origin="fake", title=event.summary,
                                 starts_at=event.starts_at, ends_at=event.ends_at)
        bucket = self.events.setdefault(profile_id, [])
        existing = next((e for e in bucket if e.id == event.uid), None)
        if existing is not None:
            if (existing.title, existing.starts_at, existing.ends_at) != (read.title, read.starts_at, read.ends_at):
                raise CalendarConflictError()
            return event.uid
        self.written[(profile_id, event.uid)] = event
        bucket.append(read)
        return event.uid

    def edit_externally(self, profile_id: str, uid: str, title: str) -> None:
        """Simulate someone changing the event in the provider UI."""
        bucket = self.events[profile_id]
        bucket[:] = [e.model_copy(update={"title": title}) if e.id == uid else e for e in bucket]
