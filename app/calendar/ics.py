"""Read-only calendar from .ics text (the synthetic `calendar.ics`) and a merged view.

Needs `icalendar` and `recurring-ical-events`. Floating times use `default_tz`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from hashlib import sha256
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.calendar.ports import CalendarEvent, CalendarPort, CalendarPermanentError, EventSpec

if TYPE_CHECKING:
    from icalendar import Event


def _moment(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)).astimezone(UTC)
    return datetime.combine(value, time.min, tzinfo=zone).astimezone(UTC)


def read_occurrence(part: Event, zone: ZoneInfo, *, origin: str) -> CalendarEvent | None:
    if str(part.get("STATUS", "")).upper() == "CANCELLED":
        return None
    begin = part.start
    uid = str(part["UID"])
    starts = _moment(begin, zone)
    ends = _moment(part.end, zone)
    instance_id = uid
    if part.get("RECURRENCE-ID") is not None:
        instance_id = f"{uid}:{starts.isoformat()}"
    if len(instance_id) > 128:
        instance_id = sha256(instance_id.encode()).hexdigest()
    return CalendarEvent(
        id=instance_id, uid=uid if len(uid) <= 128 else sha256(uid.encode()).hexdigest(),
        origin=origin, title=(str(part.get("SUMMARY") or "Без названия"))[:500],
        starts_at=starts, ends_at=ends, all_day=not isinstance(begin, datetime),
        busy=str(part.get("TRANSP", "")).upper() != "TRANSPARENT",
    )


def within_window(event: CalendarEvent, start: datetime, end: datetime) -> bool:
    if event.ends_at is None or event.ends_at == event.starts_at:
        return start <= event.starts_at < end
    return event.starts_at < end and event.ends_at > start


class IcsCalendar:
    """profile_id -> .ics text. Recurring events are expanded for the requested range."""

    def __init__(self, sources: Mapping[str, str], *, default_tz: str = "UTC",
                 timezones: Mapping[str, str] | None = None):
        self._sources = dict(sources)
        self._tz = ZoneInfo(default_tz)
        self._zones = {profile_id: ZoneInfo(zone) for profile_id, zone in (timezones or {}).items()}

    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        import recurring_ical_events
        from icalendar import Calendar

        text = self._sources.get(profile_id)
        if text is None:
            return []
        zone = self._zones.get(profile_id, self._tz)
        series = recurring_ical_events.of(Calendar.from_ical(text))
        found = []
        for part in series.between(start.astimezone(zone), end.astimezone(zone)):
            event = read_occurrence(part, zone, origin="ics")
            if event is not None and within_window(event, start, end):
                found.append(event)
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
