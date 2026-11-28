from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.calendar.caldav_adapter import CalDavCalendar, render_ics
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
