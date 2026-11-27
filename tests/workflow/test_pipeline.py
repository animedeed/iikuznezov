from datetime import UTC, date, datetime

import pytest

from app.calendar.fake import FakeCalendar
from app.calendar.ics import IcsCalendar, MergedCalendar
from app.calendar.ports import CalendarEvent, EventSpec
from app.contracts import Decision, TelegramActor
from app.core.effects import EffectQueue
from app.workflow.evaluation import evaluate_minutes, minutes_timings
from app.workflow.followups import process_meeting_event, schedule_calendar_events, sweep_approved_minutes
from app.workflow.plan import build_plan
from app.workflow.tasks import SqliteTaskStore
from app.workflow.transport import WorkflowTransport

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly@x
DTSTAMP:20261201T000000Z
DTSTART:20261130T070000Z
DTEND:20261130T073000Z
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Планёрка
END:VEVENT
BEGIN:VEVENT
UID:holiday@x
DTSTAMP:20261201T000000Z
DTSTART;VALUE=DATE:20261208
DTEND;VALUE=DATE:20261209
SUMMARY:Выходной партнёра
END:VEVENT
END:VCALENDAR
"""
DAY = (datetime(2026, 12, 7, tzinfo=UTC), datetime(2026, 12, 9, tzinfo=UTC))


def approved(store, approvals, source_factory, model_factory, draft, ids):
    event_id = approvals.submit_transcript("p1", source_factory())
    card = process_meeting_event(store, approvals, model_factory(draft), "p1", event_id)
    approvals.decide(card.request.approval_id, TelegramActor(telegram_user_id=ids.initiator), Decision.APPROVE)
    return card


def drain(store, transport):
    queue = EffectQueue(store)
    while queue.run_once(transport) is not None:
        pass


def row(store, kind):
    with store._connection() as db:
        r = db.execute("SELECT status, error_code FROM effects WHERE kind = ?", (kind,)).fetchone()
    return (r["status"], r["error_code"])


def test_full_chain_with_calendar_down_then_next_plan(store, approvals, source_factory, model_factory, draft, ids):
    approved(store, approvals, source_factory, model_factory, draft, ids)
    sweep_approved_minutes(store)
    cal, tasks = FakeCalendar(), SqliteTaskStore(store)
    from app.calendar.ports import CalendarPermanentError
    cal.fail_next(CalendarPermanentError("calendar_down"))
    drain(store, WorkflowTransport(cal, tasks))
    assert row(store, "calendar_event") == ("failed", "calendar_down")
    assert row(store, "task") == ("sent", None)
    [task] = tasks.list_open("p1")
    assert task.source.startswith("minutes:") and tasks.list_open("p2") == []
    kw = dict(store=store, calendar=FakeCalendar(), tasks=tasks)
    assert [t.id for t in build_plan("p1", date(2026, 12, 10), **kw).tasks_due] == [task.id]
    assert [t.id for t in build_plan("p1", date(2026, 12, 11), **kw).overdue] == [task.id]
    assert tasks.mark_done("p2", task.id) is False and tasks.mark_done("p1", task.id) is True
    assert build_plan("p1", date(2026, 12, 11), **kw).overdue == ()


def test_repeat_approval_and_redelivery_create_one_task(store, approvals, source_factory, model_factory, draft, ids):
    card = approved(store, approvals, source_factory, model_factory, draft, ids)
    again = approvals.decide(card.request.approval_id, TelegramActor(telegram_user_id=ids.initiator), Decision.APPROVE)
    assert again.value == "already_decided"
    tasks = SqliteTaskStore(store)
    drain(store, WorkflowTransport(FakeCalendar(), tasks))
    with store._connection(write=True) as db:
        db.execute("UPDATE effects SET status='pending', next_attempt_at=0 WHERE kind='task'")
    drain(store, WorkflowTransport(FakeCalendar(), tasks))
    assert len(tasks.list_open("p1")) == 1


def test_preference_changes_only_its_own_profile(store, approvals, source_factory, model_factory, draft, ids):
    first = approved(store, approvals, source_factory, model_factory, draft, ids)
    approvals.save_preference("p1", TelegramActor(telegram_user_id=ids.leader), "Пиши кратко",
                              first.document.document_id)
    m1, m2 = model_factory(draft), model_factory(draft)
    e1 = approvals.submit_transcript("p1", source_factory(meeting_id="m2"))
    e2 = approvals.submit_transcript("p2", source_factory(meeting_id="m2"))
    process_meeting_event(store, approvals, m1, "p1", e1)
    process_meeting_event(store, approvals, m2, "p2", e2)
    assert "Пиши кратко" in m1.calls[0][0]["content"] and "Пиши кратко" not in m2.calls[0][0]["content"]


def test_ics_expands_recurrence_keeps_all_day_and_merge_dedupes():
    ics = IcsCalendar({"p1": ICS})
    got = {e.title: e for e in ics.list_events("p1", *DAY)}
    assert set(got) == {"Планёрка", "Выходной партнёра"} and got["Выходной партнёра"].all_day
    live = FakeCalendar({"p1": [CalendarEvent(id="weekly@x", uid="weekly@x", origin="caldav", title="Планёрка",
                                              starts_at=datetime(2026, 12, 7, 7, tzinfo=UTC))]})
    merged = MergedCalendar(live, [live, ics]).list_events("p1", *DAY)
    assert sorted(e.title for e in merged) == ["Выходной партнёра", "Планёрка"]
    assert next(e for e in merged if e.title == "Планёрка").origin == "caldav"


def timed(uid, hour):
    return EventSpec(uid=uid, summary="Созвон", starts_at=datetime(2026, 12, 7, hour, tzinfo=UTC),
                     ends_at=datetime(2026, 12, 7, hour, 30, tzinfo=UTC))


def test_busy_protected_and_provider_conflict(store, approvals, source_factory, model_factory, draft, ids):
    card = approved(store, approvals, source_factory, model_factory, draft, ids)
    doc, ver = card.document.document_id, card.document.version
    live = FakeCalendar()
    calendar = MergedCalendar(live, [live, IcsCalendar({"p1": ICS})])
    protected = {"p1": [(datetime(2026, 12, 7, 12, tzinfo=UTC), datetime(2026, 12, 7, 13, tzinfo=UTC))]}
    transport = WorkflowTransport(calendar, SqliteTaskStore(store), protected=protected)
    schedule_calendar_events(store, "p1", doc, ver,
                             [timed("busy@iikuznezov", 7), timed("prot@iikuznezov", 12), timed("ok@iikuznezov", 10)])
    drain(store, transport)
    with store._connection() as db:
        got = {r["target_ref"]: (r["status"], r["error_code"])
               for r in db.execute("SELECT target_ref, status, error_code FROM effects WHERE kind='calendar_event'")}
    assert got == {"busy@iikuznezov": ("failed", "time_conflict"), "prot@iikuznezov": ("failed", "protected_interval"),
                   "ok@iikuznezov": ("sent", None)}
    live.edit_externally("p1", "ok@iikuznezov", "Переименовали вручную")
    with store._connection(write=True) as db:
        db.execute("UPDATE effects SET status='pending', next_attempt_at=0 WHERE target_ref='ok@iikuznezov'")
    drain(store, transport)
    with store._connection() as db:
        assert db.execute("SELECT error_code FROM effects WHERE target_ref='ok@iikuznezov'").fetchone()[0] == "calendar_conflict"


def test_all_day_blocks_timed_event():
    from app.calendar.busy import blocking_events
    ev = CalendarEvent(id="h", uid="h", title="Отпуск", starts_at=datetime(2026, 12, 7, tzinfo=UTC),
                       ends_at=datetime(2026, 12, 8, tzinfo=UTC), all_day=True)
    assert blocking_events([ev], datetime(2026, 12, 7, 10, tzinfo=UTC), datetime(2026, 12, 7, 11, tzinfo=UTC)) == [ev]


def test_evaluation_scores_decisions_assignees_and_dates(store, approvals, source_factory, model_factory, draft, ids):
    card = approved(store, approvals, source_factory, model_factory, draft, ids)
    reference = {"decisions": ["Подготовить КП", "Согласовать бюджет"],
                 "tasks": [{"action": "Подготовить черновик КП", "assignee_ref": "sales-desk", "due_date": "2026-12-11"}]}
    score = evaluate_minutes(card.document.candidate, reference)
    assert score.decision_recall == 0.5 and score.decision_precision == 1.0
    assert score.task_recall == 1.0 and score.assignee_accuracy == 1.0 and score.due_accuracy == 0.0
    t = minutes_timings(store, "p1", card.document.document_id, card.document.version)
    assert t["seconds_to_draft"] >= 0 and t["seconds_to_decision"] >= 0
    with pytest.raises(LookupError):
        minutes_timings(store, "p2", card.document.document_id, card.document.version)
