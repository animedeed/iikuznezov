"""CalDAV adapter (Yandex Calendar). Blocking: call it outside the event loop.

Needs the optional `caldav` package. NOT verified against a live Yandex account:
run the calendar access check from TEAM_PLAN before marking this channel as live.
Credentials are passed in by the caller (read them from environment variables there).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta

from app.calendar.ports import (
    CalendarConflictError,
    CalendarEvent,
    CalendarPermanentError,
    EventSpec,
)


def _escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def render_ics(event: EventSpec, *, stamp: datetime | None = None) -> str:
    """Minimal RFC 5545 VEVENT. Deterministic apart from DTSTAMP."""
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//iikuznezov//workflow//RU",
        "BEGIN:VEVENT", f"UID:{event.uid}", f"DTSTAMP:{_utc(stamp or datetime.now(UTC))}",
        f"SUMMARY:{_escape(event.summary)}",
    ]
    if event.start_date is not None:
        lines += [f"DTSTART;VALUE=DATE:{event.start_date:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{event.start_date + timedelta(days=1):%Y%m%d}"]
    else:
        lines += [f"DTSTART:{_utc(event.starts_at)}", f"DTEND:{_utc(event.ends_at)}"]
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


class CalDavCalendar:
    def __init__(self, *, url: str, username: str, password: str,
                 calendars: Mapping[str, str], timeout: int = 10):
        """`calendars` maps profile_id -> calendar display name."""
        self._conn = (url, username, password, timeout)
        self._names = dict(calendars)
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
        found = calendar.search(start=start, end=end, event=True, expand=True)
        events: list[CalendarEvent] = []
        for item in found:
            for part in item.icalendar_instance.walk("VEVENT"):
                begin = part.get("DTSTART").dt
                finish = part.get("DTEND").dt if part.get("DTEND") is not None else None
                all_day = isinstance(begin, date) and not isinstance(begin, datetime)
                if all_day:
                    begin = datetime.combine(begin, time.min, tzinfo=UTC)
                    if isinstance(finish, date) and not isinstance(finish, datetime):
                        finish = datetime.combine(finish, time.min, tzinfo=UTC)
                elif begin.tzinfo is None:
                    begin = begin.replace(tzinfo=UTC)
                if isinstance(finish, datetime) and finish.tzinfo is None:
                    finish = finish.replace(tzinfo=UTC)
                events.append(CalendarEvent(
                    id=str(part.get("UID")), uid=str(part.get("UID")), origin="caldav", title=str(part.get("SUMMARY") or "Без названия")[:500],
                    starts_at=begin, ends_at=finish, all_day=all_day,
                ))
        return events

    def upsert_event(self, profile_id: str, event: EventSpec) -> str:
        calendar = self._calendar(profile_id)
        try:
            existing = calendar.event_by_uid(event.uid)
        except Exception as error:  # caldav raises NotFoundError for a missing UID
            if type(error).__name__ != "NotFoundError":
                raise
            existing = None
        if existing is not None:
            if self._same(existing, event):
                return event.uid
            raise CalendarConflictError()
        calendar.save_event(render_ics(event))
        return event.uid

    @staticmethod
    def _same(existing, event: EventSpec) -> bool:
        part = next(iter(existing.icalendar_instance.walk("VEVENT")), None)
        if part is None or str(part.get("SUMMARY")) != event.summary:
            return False
        begin = part.get("DTSTART").dt
        if event.start_date is not None:
            return begin == event.start_date
        return isinstance(begin, datetime) and begin.astimezone(UTC) == event.starts_at
