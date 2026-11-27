"""Read-only calendar from .ics text (the synthetic `calendar.ics`) and a merged view.

Needs `icalendar` and `recurring-ical-events`. Floating times use `default_tz`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.calendar.ports import CalendarEvent, CalendarPort, CalendarPermanentError, EventSpec


class IcsCalendar:
    """profile_id -> .ics text. Recurring events are expanded for the requested range."""

    def __init__(self, sources: Mapping[str, str], *, default_tz: str = "UTC"):
        self._sources = dict(sources)
        self._tz = ZoneInfo(default_tz)

    def _moment(self, value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=self._tz)).astimezone(UTC)
        return datetime.combine(value, time.min, tzinfo=self._tz).astimezone(UTC)

    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        import recurring_ical_events
        from icalendar import Calendar

        text = self._sources.get(profile_id)
        if text is None:
            return []
        found = []
        for part in recurring_ical_events.of(Calendar.from_ical(text)).between(start, end):
            begin = part.get("DTSTART").dt
            finish = part.get("DTEND").dt if part.get("DTEND") is not None else None
            all_day = isinstance(begin, date) and not isinstance(begin, datetime)
            starts = self._moment(begin)
            ends = self._moment(finish) if finish is not None else (
                starts + timedelta(days=1) if all_day else starts)
            uid = str(part.get("UID"))
            found.append(CalendarEvent(
                id=f"{uid}:{starts:%Y%m%dT%H%M}" if part.get("RECURRENCE-ID") is not None else uid,
                uid=uid, origin="ics", title=(str(part.get("SUMMARY") or "Без названия"))[:500],
                starts_at=starts, ends_at=ends, all_day=all_day,
            ))
        return found

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        raise CalendarPermanentError("calendar_read_only")


class MergedCalendar:
    """Writes go to `writer`; reads combine all readers, one entry per (UID, start).
    Earlier readers win, so list the live calendar before the .ics set."""

    def __init__(self, writer: CalendarPort, readers: Sequence[CalendarPort]):
        self.writer, self.readers = writer, tuple(readers)

    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        seen, merged = set(), []
        for reader in self.readers:
            for event in reader.list_events(profile_id, start, end):
                key = (event.uid or event.id, event.starts_at)
                if key not in seen:
                    seen.add(key)
                    merged.append(event)
        return merged

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        return self.writer.upsert_event(profile_id, event)
