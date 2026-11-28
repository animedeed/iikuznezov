from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.calendar.busy import blocking_events
from app.calendar.caldav_adapter import CalDavCalendar, render_ics
from app.calendar.ics import IcsCalendar
from app.calendar.ports import CalendarConflictError, EventSpec


def test_caldav_creation_is_conditional_and_replay_compares_all_fields():
    icalendar = pytest.importorskip("icalendar")
    errors = pytest.importorskip("caldav.lib.error")

    class Calendar:
        def __init__(self):
            self.writes = []
            self.saved = None

        def event_by_uid(self, uid):
            if self.saved is None:
                raise errors.NotFoundError("missing")
            return self.saved

        def add_event(self, ics, *, no_overwrite):
            assert no_overwrite
            self.writes.append(ics)
            self.saved = SimpleNamespace(icalendar_instance=icalendar.Calendar.from_ical(ics))

    calendar = Calendar()
    adapter = CalDavCalendar(url="https://example.invalid", username="test", password="test",
                            calendars={"p1": "fixture"})
    adapter._cache["p1"] = calendar
    start = datetime(2026, 12, 8, 12, tzinfo=UTC)
    event = EventSpec(uid="test@iikuznezov", summary="Тест", description="Контекст",
                      starts_at=start, ends_at=start + timedelta(minutes=30))
    assert adapter.upsert_event("p1", event) == event.uid
    assert adapter.upsert_event("p1", event) == event.uid
    assert len(calendar.writes) == 1
    changed = event.model_copy(update={"ends_at": start + timedelta(hours=1)})
    calendar.saved = SimpleNamespace(icalendar_instance=icalendar.Calendar.from_ical(render_ics(changed)))
    with pytest.raises(CalendarConflictError):
        adapter.upsert_event("p1", event)
    assert len(calendar.writes) == 1


def test_caldav_precondition_conflict_reads_the_existing_uid():
    icalendar = pytest.importorskip("icalendar")
    errors = pytest.importorskip("caldav.lib.error")
    event = EventSpec(uid="race@iikuznezov", summary="Срок", start_date="2026-12-09")

    class Calendar:
        existing = None

        def event_by_uid(self, uid):
            if not self.existing:
                raise errors.NotFoundError("missing")
            return self.existing

        def add_event(self, ics, *, no_overwrite):
            assert no_overwrite
            self.existing = SimpleNamespace(icalendar_instance=icalendar.Calendar.from_ical(ics))
            raise errors.ConsistencyError("concurrent write")

    adapter = CalDavCalendar(url="https://example.invalid", username="test", password="test",
                            calendars={"p1": "fixture"})
    adapter._cache["p1"] = Calendar()
    assert adapter.upsert_event("p1", event) == event.uid


def test_caldav_sends_conditional_put_to_stable_resource(monkeypatch):
    caldav = pytest.importorskip("caldav")
    errors = pytest.importorskip("caldav.lib.error")
    client = caldav.DAVClient(url="https://example.invalid/")
    calendar = caldav.Calendar(client=client, url="https://example.invalid/cal/")
    writes = []

    def missing(uid):
        raise errors.NotFoundError(uid)

    def put(url, body, headers):
        writes.append((str(url), headers["if-none-match"]))
        return SimpleNamespace(status=201, headers={})

    monkeypatch.setattr(calendar, "event_by_uid", missing)
    monkeypatch.setattr(calendar, "get_event_by_uid", missing)
    monkeypatch.setattr(client, "put", put)
    adapter = CalDavCalendar(url="https://example.invalid", username="test", password="test",
                            calendars={"p1": "fixture"})
    adapter._cache["p1"] = calendar
    event = EventSpec(uid="conditional@iikuznezov", summary="Срок", start_date="2026-12-09")
    assert adapter.upsert_event("p1", event) == event.uid
    assert writes == [("https://example.invalid/cal/conditional%40iikuznezov.ics", "*")]


def test_floating_and_all_day_events_use_profile_timezone_for_both_readers():
    icalendar = pytest.importorskip("icalendar")
    pytest.importorskip("recurring_ical_events")
    data = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//fixture//EN",
        "BEGIN:VEVENT", "UID:holiday", "DTSTART;VALUE=DATE:20260308", "SUMMARY:Holiday", "END:VEVENT",
        "BEGIN:VEVENT", "UID:reminder@iikuznezov", "DTSTART:20260308T080000", "DURATION:PT1H",
        "SUMMARY:Check", "END:VEVENT",
        "BEGIN:VEVENT", "UID:available", "DTSTART:20260308T080000", "DURATION:PT1H",
        "TRANSP:TRANSPARENT", "END:VEVENT",
        "BEGIN:VEVENT", "UID:cancelled", "DTSTART:20260308T080000", "DURATION:PT1H",
        "STATUS:CANCELLED", "END:VEVENT",
        "BEGIN:VEVENT", "UID:next-day", "DTSTART:20260309T000000", "DURATION:PT1H", "END:VEVENT",
        "END:VCALENDAR", "",
    ])
    times = {"p1": "America/New_York"}
    live = CalDavCalendar(url="https://example.invalid", username="test", password="test",
                          calendars={"p1": "fixture"}, timezones=times)
    live._cache["p1"] = SimpleNamespace(search=lambda **_: [
        SimpleNamespace(icalendar_instance=icalendar.Calendar.from_ical(data)),
    ])
    local = IcsCalendar({"p1": data}, timezones=times)
    start = datetime(2026, 3, 8, 5, tzinfo=UTC)
    end = datetime(2026, 3, 9, 4, tzinfo=UTC)  # New York's spring-forward day is 23 hours.
    for calendar in (live, local):
        entries = {event.uid: event for event in calendar.list_events("p1", start, end)}
        assert entries.keys() == {"holiday", "reminder@iikuznezov", "available"}
        assert (entries["holiday"].starts_at, entries["holiday"].ends_at) == (start, end)
        busy_start = datetime(2026, 3, 8, 12, tzinfo=UTC)
        busy_end = busy_start + timedelta(hours=1)
        assert entries["reminder@iikuznezov"].starts_at == busy_start
        assert entries["reminder@iikuznezov"].ends_at == busy_end
        assert {event.uid for event in blocking_events(tuple(entries.values()), busy_start, busy_end)} == {
            "holiday", "reminder@iikuznezov",
        }


def test_ics_serialization_round_trips_multiline_text():
    icalendar = pytest.importorskip("icalendar")
    spec = EventSpec(uid="serialize@example.invalid", summary="Длинное название " * 12,
                     description="Первая строка\r\nДальше запятая, точка с запятой; и слеш \\",
                     start_date="2026-12-09")
    serialized = render_ics(spec)
    assert all(len(line.encode()) <= 75 for line in serialized.splitlines())
    existing = SimpleNamespace(icalendar_instance=icalendar.Calendar.from_ical(serialized))
    assert CalDavCalendar._same(existing, spec)
