import pytest

from app.calendar.fake import FakeCalendar
from app.calendar.ports import CalendarPermanentError, CalendarTemporaryError
from app.contracts import Decision, TelegramActor
from app.core.effects import EffectQueue
from app.workflow.followups import process_meeting_event, sweep_approved_minutes
from app.workflow.tasks import FakeTaskSink
from app.workflow.transport import WorkflowTransport


@pytest.fixture
def queue(store, approvals, source_factory, model_factory, draft, ids):
    event_id = approvals.submit_transcript("p1", source_factory())
    card = process_meeting_event(store, approvals, model_factory(draft), "p1", event_id)
    approvals.decide(card.request.approval_id, TelegramActor(telegram_user_id=ids.initiator), Decision.APPROVE)
    sweep_approved_minutes(store)
    return EffectQueue(store)


def statuses(store):
    with store._connection() as db:
        return {r["kind"] + ":" + r["target_ref"]: (r["status"], r["error_code"])
                for r in db.execute("SELECT kind, target_ref, status, error_code FROM effects")}


def calendar_row(store):
    return next(v for k, v in statuses(store).items() if k.startswith("calendar_event"))


def drain(queue, transport):
    while queue.run_once(transport) is not None:
        pass


def test_task_and_calendar_are_delivered_once(store, queue):
    cal, sink = FakeCalendar(), FakeTaskSink()
    drain(queue, WorkflowTransport(cal, sink))
    assert statuses(store)["task:task-1"][0] == "sent" and calendar_row(store)[0] == "sent"
    assert cal.upsert_calls == 1 and sink.calls == 1 and len(cal.written) == 1


def test_calendar_failure_does_not_undo_the_task(store, queue):
    cal = FakeCalendar()
    cal.fail_next(CalendarPermanentError("calendar_not_found"))
    drain(queue, WorkflowTransport(cal, FakeTaskSink()))
    assert statuses(store)["task:task-1"][0] == "sent"
    assert calendar_row(store) == ("failed", "calendar_not_found")


def test_temporary_error_is_retryable(store, queue):
    cal = FakeCalendar()
    cal.fail_next(CalendarTemporaryError("calendar_busy", retry_after_seconds=5))
    drain(queue, WorkflowTransport(cal, FakeTaskSink()))
    assert calendar_row(store) == ("retryable", "calendar_busy")


def test_ambiguous_exception_becomes_unknown(store, queue):
    cal = FakeCalendar()
    cal.fail_next(TimeoutError())  # the write may or may not have reached the provider
    drain(queue, WorkflowTransport(cal, FakeTaskSink()))
    assert calendar_row(store) == ("unknown", "transport_exception")
