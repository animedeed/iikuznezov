from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.resources import files
from sqlite3 import connect

import pytest

from app.contracts import (
    Decision,
    DecisionStatus,
    DeliveryStatus,
    EffectKind,
    EffectResult,
    MailCandidate,
    MailDispatch,
    MailReceipt,
    MinutesCandidate,
    Profile,
    Route,
    TaskDraft,
    TelegramActor,
)
from app.core.approvals import ApprovalService, EffectIntent, MeetingSource
from app.core.demo import seed_demo
from app.core.effects import EffectQueue
from app.core.store import Store
from app.testing import FakeTransport

P1 = "demo-p1"
P2 = "demo-p2"
LEADER = TelegramActor(telegram_user_id=10001)
OTHER = TelegramActor(telegram_user_id=10002)
INITIATOR = TelegramActor(telegram_user_id=10005)
RECEIPT = MailReceipt.model_validate({
    "folder": "INBOX", "uid": 7, "uidvalidity": 12, "from": "mail@example.invalid",
    "subject": "Срок", "text": "Подтвердите срок", "received_at": "2026-12-07T07:00:00Z",
})
CANDIDATE = MailCandidate(
    topic="terms", requires_review=True, rationale="Подтверждение срока", draft_text="Срок уточняется.",
)
DISPATCH = MailDispatch(action="reply", target_ref="customer-binding-v1")
INTENT = EffectIntent(kind=EffectKind.EMAIL, target_ref=DISPATCH.target_ref)


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "stage2.sqlite3")
    db.initialize()
    seed_demo(db)
    return db


def propose(store, *, intents=(INTENT,)):
    source = store.receive_mail("demo-mail-sales", RECEIPT)
    return ApprovalService(store).propose(
        P1, source.event_id, CANDIDATE, mail_dispatch=DISPATCH, intents=intents,
    )


def test_callback_persists_version_actor_comment_and_one_effect(store):
    card = propose(store)
    assert propose(store) == card
    service = ApprovalService(store)
    callback = card.request.approval_id
    assert service.get_pending(P1, callback, LEADER) == card
    assert service.get_pending(P2, callback, OTHER) is None
    assert service.get_pending(P1, callback, OTHER) is None
    assert service.decide(callback, OTHER, Decision.APPROVE) == DecisionStatus.FORBIDDEN
    assert service.decide(callback, LEADER, Decision.APPROVE, "x" * 2001) == DecisionStatus.VALIDATION_ERROR
    queue = EffectQueue(store)
    transport = FakeTransport(EffectResult(status=DeliveryStatus.SENT))
    assert queue.run_once(transport)
    assert transport.calls.pop().kind == EffectKind.TELEGRAM
    assert queue.run_once(transport) is None
    assert service.decide(callback, LEADER, Decision.APPROVE, "Проверено") == DecisionStatus.ACCEPTED

    reopened = Store(store.path)
    reopened.initialize()
    service = ApprovalService(reopened)
    saved = service.get_decision(P1, callback, LEADER)
    assert saved.decision == Decision.APPROVE and saved.decided_by == LEADER.telegram_user_id
    assert saved.comment == "Проверено" and saved.version == 1
    assert saved.decided_at.tzinfo == UTC
    assert service.get_decision(P2, callback, OTHER) is None
    assert service.decide(callback, LEADER, Decision.REJECT) == DecisionStatus.ALREADY_DECIDED
    assert EffectQueue(reopened).run_once(transport) is not None
    assert EffectQueue(reopened).run_once(transport) is None
    assert len(transport.calls) == 1
    assert transport.calls[0].target_ref == DISPATCH.target_ref
    assert transport.calls[0].version == 1
    assert transport.calls[0].payload["draft_text"] == CANDIDATE.draft_text
    with reopened._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approval_audit WHERE event='decided'").fetchone()[0] == 1


def test_concurrent_buttons_have_one_winner(store):
    card = propose(store)
    service = ApprovalService(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(
            lambda _: service.decide(card.request.approval_id, LEADER, Decision.APPROVE), range(2),
        ))
    assert sorted(result) == [DecisionStatus.ACCEPTED, DecisionStatus.ALREADY_DECIDED]
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM effects WHERE kind = 'email'").fetchone()[0] == 1


@pytest.mark.parametrize("decision", [Decision.REJECT, Decision.REVISE])
def test_reject_or_revise_persists_without_delivery(store, decision):
    card = propose(store)
    service = ApprovalService(store)
    callback = card.request.approval_id
    assert service.decide(callback, LEADER, decision) == DecisionStatus.ACCEPTED
    assert service.get_decision(P1, callback, LEADER).decision == decision
    assert EffectQueue(store).run_once(FakeTransport(EffectResult(status=DeliveryStatus.SENT))) is None


def test_new_version_invalidates_card_and_cancels_unstarted_delivery(store):
    card = propose(store)
    service = ApprovalService(store)
    callback = card.request.approval_id
    assert service.decide(callback, LEADER, Decision.APPROVE) == DecisionStatus.ACCEPTED
    new_dispatch = MailDispatch(action="reply", target_ref="customer-binding-v2")
    updated = service.revise_document(
        P1, card.document.document_id, LEADER, 1, CANDIDATE, mail_dispatch=new_dispatch,
        intents=(EffectIntent(kind=EffectKind.EMAIL, target_ref=new_dispatch.target_ref),),
    )
    assert updated.document.version == 2 and updated.request.approval_id != callback
    assert service.decide(callback, LEADER, Decision.APPROVE) == DecisionStatus.STALE
    assert service.get_decision(P1, callback, LEADER).decision == Decision.APPROVE
    transport = FakeTransport(EffectResult(status=DeliveryStatus.SENT))
    queue = EffectQueue(store)
    assert queue.run_once(transport)
    assert transport.calls.pop().kind == EffectKind.TELEGRAM
    assert queue.run_once(transport) is None
    assert service.decide(updated.request.approval_id, LEADER, Decision.APPROVE) == DecisionStatus.ACCEPTED
    assert queue.run_once(transport) is not None
    assert [e.target_ref for e in transport.calls] == [new_dispatch.target_ref]


def test_revision_after_delivery_started_requires_separate_correction(store):
    card = propose(store)
    service = ApprovalService(store)
    assert service.decide(card.request.approval_id, LEADER, Decision.APPROVE) == DecisionStatus.ACCEPTED
    assert EffectQueue(store).run_once(FakeTransport(EffectResult(status=DeliveryStatus.UNKNOWN)))
    with pytest.raises(ValueError, match="separate correction"):
        service.revise_document(P1, card.document.document_id, LEADER, 1, CANDIDATE)


def test_replaced_leader_loses_access_to_old_card(store):
    card = propose(store)
    store.seed_profiles((
        (Profile(profile_id=P1, timezone="Europe/Moscow", quote_route=Route.REVIEW),
         "demo-mail-sales", 10003),
    ))
    assert ApprovalService(store).decide(card.request.approval_id, LEADER, Decision.APPROVE) == DecisionStatus.FORBIDDEN


def test_failed_queue_write_rolls_back_decision_and_audit(store, monkeypatch):
    card = propose(store)
    service = ApprovalService(store)

    def unavailable(_db, _effect):
        raise RuntimeError("Test fault")

    monkeypatch.setattr("app.core.approvals.enqueue_effect", unavailable)
    with pytest.raises(RuntimeError, match="Test fault"):
        service.decide(card.request.approval_id, LEADER, Decision.APPROVE)
    assert service.get_pending(P1, card.request.approval_id, LEADER) == card
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approval_audit WHERE event='decided'").fetchone()[0] == 0


def test_meeting_initiator_only_and_incomplete_task_never_queued(store):
    service = ApprovalService(store)
    source = service.submit_transcript(P1, MeetingSource(
        meeting_id="meeting-1", initiator=INITIATOR, participant_refs=("recipient",), turns=(),
    ))
    assert service.submit_transcript(P1, MeetingSource(
        meeting_id="meeting-1", initiator=INITIATOR, participant_refs=("recipient",), turns=(),
    )) == source
    candidate = MinutesCandidate(
        meeting_id="meeting-1", title="Тест", occurred_at=datetime(2026, 12, 7, tzinfo=UTC),
        participant_refs=("recipient",), summary="Итог", decisions=(), open_questions=(),
        tasks=(TaskDraft(
            task_ref="unassigned", action="Уточнить", assignee_ref=None,
            due_date=None, status="needs_clarification",
        ),),
    )
    with pytest.raises(ValueError, match="no complete approved task"):
        service.propose(P1, source, candidate, intents=(EffectIntent(kind=EffectKind.TASK, target_ref="unassigned"),))
    card = service.propose(P1, source, candidate, intents=(EffectIntent(kind=EffectKind.EMAIL, target_ref="recipient"),))
    assert service.decide(card.request.approval_id, LEADER, Decision.APPROVE) == DecisionStatus.FORBIDDEN
    assert service.decide(card.request.approval_id, INITIATOR, Decision.APPROVE) == DecisionStatus.ACCEPTED


def test_explicit_preference_is_scoped_and_repeatable(store):
    card = propose(store)
    service = ApprovalService(store)
    document_id = card.document.document_id
    first = service.save_preference(P1, LEADER, "Сначала сроки", document_id)
    assert service.save_preference(P1, LEADER, "Сначала сроки", document_id) == first
    with pytest.raises(PermissionError):
        service.save_preference(P1, OTHER, "Иной стиль", document_id)
    with pytest.raises(LookupError):
        service.save_preference(P2, OTHER, "Иной стиль", document_id)


def test_migration_upgrades_existing_v1_database(tmp_path):
    path = tmp_path / "v1.sqlite3"
    with connect(path) as db:
        db.executescript(files("migrations").joinpath("001_initial.sql").read_text(encoding="utf-8"))
    store = Store(path)
    seed_demo(store)
    draft = store.record_draft(P1, store.receive_mail("demo-mail-sales", RECEIPT).event_id, CANDIDATE)
    store.initialize()
    store.initialize()
    assert store.get_version(P1, draft.document_id, 1) == draft
    with store._connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] >= 3


def test_opening_saves_one_notice_atomically_and_replay_does_not_duplicate(store):
    card = propose(store)
    assert propose(Store(store.path)) == card
    with store._connection() as db:
        registered = db.execute(
            "SELECT status FROM inbox_events WHERE event_id = ?", (card.document.source_id,),
        ).fetchone()
        assert registered["status"] == "processed"
        rows = db.execute("SELECT kind, status, target_ref FROM effects").fetchall()
        assert [tuple(row) for row in rows] == [("telegram", "pending", card.request.approval_id)]


def test_revoked_leader_does_not_receive_pending_notice(store):
    propose(store)
    store.seed_profiles((
        (Profile(profile_id=P1, timezone="Europe/Moscow", quote_route=Route.REVIEW),
         "demo-mail-sales", 10003),
    ))
    transport = FakeTransport(EffectResult(status=DeliveryStatus.SENT))
    assert EffectQueue(store).run_once(transport) is None
    assert not transport.calls


def test_rejects_delivery_target_different_from_version(store):
    with pytest.raises(ValueError, match="does not match"):
        propose(store, intents=(EffectIntent(kind=EffectKind.EMAIL, target_ref="wrong-address"),))
    with store._connection() as db:
        assert not db.execute("SELECT 1 FROM approvals").fetchall()
        assert not db.execute("SELECT 1 FROM effects").fetchall()


def test_task_payload_contains_only_frozen_authorized_assignment(store):
    service = ApprovalService(store)
    source = service.submit_transcript(P1, MeetingSource(
        meeting_id="meeting-2", initiator=INITIATOR, participant_refs=("recipient",),
        allowed_assignee_refs=("sales-staff",), turns=(),
    ))
    task = TaskDraft(
        task_ref="task-1", action="Подготовить КП", assignee_ref="sales-staff",
        due_date="2026-12-10", status="draft",
    )
    candidate = MinutesCandidate(
        meeting_id="meeting-2", title="Тест", occurred_at=datetime(2026, 12, 7, tzinfo=UTC),
        participant_refs=("recipient",), summary="Итог", decisions=(), open_questions=(),
        tasks=(task,),
    )
    intent = EffectIntent(kind=EffectKind.TASK, target_ref="task-1")
    unknown = task.model_copy(update={"assignee_ref": "invented-person"})
    with pytest.raises(ValueError, match="no complete approved task"):
        service.propose(P1, source, candidate.model_copy(update={"tasks": (unknown,)}), intents=(intent,))
    card = service.propose(P1, source, candidate, intents=(intent,))
    assert service.decide(card.request.approval_id, INITIATOR, Decision.APPROVE) == DecisionStatus.ACCEPTED
    transport = FakeTransport(EffectResult(status=DeliveryStatus.SENT))
    assert EffectQueue(store).run_once(transport)
    assert transport.calls[0].payload == {"task": task.model_dump(mode="json")}

