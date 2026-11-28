"""CalDAV adapter (Yandex Calendar). Blocking: call it outside the event loop.

Needs the optional `caldav` package. NOT verified against a live Yandex account:
run the calendar access check from TEAM_PLAN before marking this channel as live.
Credentials are passed in by the caller (read them from environment variables there).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.calendar.ics import read_occurrence, within_window
from app.calendar.ports import (
    CalendarConflictError,
    CalendarEvent,
    CalendarPermanentError,
    EventSpec,
)


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def render_ics(event: EventSpec, *, stamp: datetime | None = None) -> str:
    """Minimal RFC 5545 VEVENT. Deterministic apart from DTSTAMP."""
    from icalendar import Calendar, Event

    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//iikuznezov//workflow//RU")
    component = Event()
    component.add("UID", event.uid)
    component.add("DTSTAMP", (stamp or datetime.now(UTC)).astimezone(UTC))
    component.add("SUMMARY", _normalize_newlines(event.summary))
    if event.start_date is not None:
        component.add("DTSTART", event.start_date)
        component.add("DTEND", event.start_date + timedelta(days=1))
    else:
        component.add("DTSTART", event.starts_at)
        component.add("DTEND", event.ends_at)
    if event.description:
        component.add("DESCRIPTION", _normalize_newlines(event.description))
    calendar.add_component(component)
    return calendar.to_ical().decode("utf-8")


class CalDavCalendar:
    def __init__(self, *, url: str, username: str, password: str,
                 calendars: Mapping[str, str], timeout: int = 10,
                 timezones: Mapping[str, str] | None = None):
        """`calendars` maps profile IDs to display names, `timezones` to IANA names."""
        self._conn = (url, username, password, timeout)
        self._names = dict(calendars)
        self._zones = {profile_id: ZoneInfo(zone) for profile_id, zone in (timezones or {}).items()}
        self._cache: dict[str, object] = {}

    def _calendar(self, profile_id: str):
        if profile_id not in self._names:
            raise CalendarPermanentError("calendar_not_configured")
        if profile_id not in self._cache:
            import caldav  # optional dependency

            url, username, password, timeout = self._conn
            principal = caldav.DAVClient(url=url, username=username, password=password,
                                         timeout=timeout).principal()
            matches = [c for c in principal.calendars() if c.name == self._names[profile_id]]
            if len(matches) != 1:
                raise CalendarPermanentError("calendar_not_found")
            self._cache[profile_id] = matches[0]
        return self._cache[profile_id]

    def list_events(self, profile_id: str, start: datetime, end: datetime) -> Sequence[CalendarEvent]:
        calendar = self._calendar(profile_id)
        zone = self._zones.get(profile_id, ZoneInfo("UTC"))
        found = calendar.search(start=start.astimezone(zone), end=end.astimezone(zone), event=True, expand=True)
        events: list[CalendarEvent] = []
        for item in found:
            for part in item.icalendar_instance.walk("VEVENT"):
                event = read_occurrence(part, zone, origin="caldav")
                if event is not None and within_window(event, start, end):
                    events.append(event)
        return events

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        from caldav.lib.error import ConsistencyError, NotFoundError

        calendar = self._calendar(profile_id)
        try:
            existing = calendar.event_by_uid(event.uid)
        except NotFoundError:
            try:
                calendar.add_event(render_ics(event), no_overwrite=True)
            except ConsistencyError:
                try:
                    existing = calendar.event_by_uid(event.uid)
                except NotFoundError:
                    raise CalendarConflictError() from None
            else:
                return event.uid
        if not self._same(existing, event):
            raise CalendarConflictError()
        return event.uid

    @staticmethod
    def _same(existing, event: EventSpec) -> bool:
        parts = existing.icalendar_instance.walk("VEVENT")
        if len(parts) != 1:
            return False
        part = parts[0]
        if any(part.get(key) is not None for key in ("RRULE", "RDATE", "RECURRENCE-ID", "ATTENDEE")):
            return False
        if str(part.get("STATUS", "")).upper() == "CANCELLED":
            return False
        if str(part.get("UID")) != event.uid or str(part.get("SUMMARY")) != _normalize_newlines(event.summary):
            return False
        if str(part.get("DESCRIPTION") or "") != _normalize_newlines(event.description):
            return False
        begin = part.decoded("DTSTART", None)
        end = part.decoded("DTEND", None)
        if event.start_date is not None:
            return begin == event.start_date and end == event.start_date + timedelta(days=1)
        if (not isinstance(begin, datetime) or not isinstance(end, datetime)
                or begin.tzinfo is None or end.tzinfo is None):
            return False
        return (
            begin.astimezone(UTC) == event.starts_at.replace(microsecond=0)
            and end.astimezone(UTC) == event.ends_at.replace(microsecond=0)
        )
