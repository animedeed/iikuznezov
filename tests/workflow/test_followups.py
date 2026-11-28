import pytest

from app.contracts import Decision, TelegramActor
from app.workflow.followups import (
    ApprovalRequired, deadline_events, load_meeting_source, process_meeting_event,
    schedule_calendar_events, sweep_approved_minutes,
)


@pytest.fixture
def opened(store, approvals, source_factory, model_factory, draft):
    event_id = approvals.submit_transcript("p1", source_factory())
    model = model_factory(draft)
    return event_id, model, process_meeting_event(store, approvals, model, "p1", event_id)


@pytest.fixture
def approve(approvals, ids):
    return lambda card: approvals.decide(
        card.request.approval_id, TelegramActor(telegram_user_id=ids.initiator), Decision.APPROVE)


def effects(store):
    with store._connection() as db:
        rows = db.execute("SELECT kind, status, target_ref FROM effects ORDER BY created_at, effect_id").fetchall()
    return [tuple(r) for r in rows if r["kind"] != "telegram"]  # skip the approval notice


def test_process_opens_one_approval_and_no_effects_before_decision(store, approvals, opened):
    event_id, model, card = opened
    assert card.request.actor_ref == "meeting-initiator"
    assert effects(store) == []
    again = process_meeting_event(store, approvals, model, "p1", event_id)
    assert again.request.approval_id == card.request.approval_id and len(model.calls) == 1


def test_approve_queues_emails_and_only_complete_tasks(store, opened, approve):
    _, _, card = opened
    assert approve(card).value == "accepted"
    assert sorted(effects(store)) == sorted([
        ("email", "pending", "meeting-initiator"), ("email", "pending", "sales-desk"),
        ("task", "pending", "task-1"),
    ])


def test_calendar_needs_approval_then_is_idempotent(store, opened, approve):
    event_id, _, card = opened
    doc, ver = card.document.document_id, card.document.version
    source, _ = load_meeting_source(store, "p1", event_id)
    specs = deadline_events("p1", doc, ver, card.document.candidate, source.allowed_assignee_refs)
    assert len(specs) == 1 and specs[0].start_date.isoformat() == "2026-12-10"
    with pytest.raises(ApprovalRequired):
        schedule_calendar_events(store, "p1", doc, ver, specs)
    approve(card)
    first = schedule_calendar_events(store, "p1", doc, ver, specs)
    assert schedule_calendar_events(store, "p1", doc, ver, specs) == first
    assert [e for e in effects(store) if e[0] == "calendar_event"] == [("calendar_event", "pending", specs[0].uid)]


def test_sweep_skips_unapproved_and_is_repeatable(store, opened, approve):
    _, _, card = opened
    assert sweep_approved_minutes(store) == 0
    approve(card)
    assert sweep_approved_minutes(store) == 1 and sweep_approved_minutes(store) == 1
    assert len([e for e in effects(store) if e[0] == "calendar_event"]) == 1


def test_sweep_can_be_scoped_to_one_profile(store, opened, approve):
    _, _, card = opened
    approve(card)
    assert sweep_approved_minutes(store, profile_id="p2") == 0
    assert sweep_approved_minutes(store, profile_id="p1") == 1


def test_wrong_profile_cannot_read_meeting(store, opened):
    event_id, _, _ = opened
    with pytest.raises(LookupError):
        load_meeting_source(store, "p2", event_id)
