from asyncio import run
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import Settings
from app.contracts import (
    ApprovalCardView,
    ApprovalRequest,
    Decision,
    DecisionStatus,
    DeliveryStatus,
    Effect,
    EffectKind,
    EffectResult,
    MailCandidate,
    MailDispatch,
    MailReceipt,
    Profile,
    Route,
    TelegramActor,
)
from app.core.demo import seed_demo, smoke
from app.core.store import BindingConflict, Store
from app.main import create_app
from app.testing import FakeApprovals, FakeModel, FakeTransport


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "foundation.sqlite3")
    result.initialize()
    seed_demo(result)
    return result


def example_mail(uid: int = 101, uidvalidity: int = 42) -> MailReceipt:
    return MailReceipt.model_validate({
        "folder": "INBOX", "uid": uid, "uidvalidity": uidvalidity,
        "from": "sender@example.invalid", "subject": "Срок",
        "text": "Подтвердите срок.", "received_at": datetime(2026, 12, 7, tzinfo=UTC),
    })


def test_health_migrates_an_empty_database(tmp_path: Path):
    path = tmp_path / "fresh" / "health.sqlite3"
    application = create_app(Settings(database_path=path))

    async def check():
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://testserver",
            ) as client:
                response = await client.get("/health")
                assert response.status_code == 200
                assert response.json() == {"status": "ok"}
                assert (await client.get("/docs")).status_code == 404
            assert path.is_file()

    run(check())


def test_seed_is_repeatable_and_profiles_differ(store: Store):
    seed_demo(store)
    assert store.get_profile("demo-p1").quote_route == Route.FORWARD_SALES
    assert store.get_profile("demo-p2").quote_route == Route.REVIEW
    assert store.get_profile("missing") is None


def test_conflicting_mail_binding_does_not_create_profile(store: Store):
    profile = Profile(profile_id="other", timezone="Europe/Moscow", quote_route=Route.REVIEW)
    with pytest.raises(BindingConflict):
        store.seed_profiles(((profile, "demo-mail-sales", 10003),))
    assert store.get_profile("other") is None
    assert store.receive_mail("demo-mail-sales", example_mail()).profile_id == "demo-p1"


def test_replaced_mail_binding_is_inactive_and_cannot_move_profile(store: Store):
    profile = store.get_profile("demo-p1")
    store.seed_profiles(((profile, "replacement", 10005),))
    with pytest.raises(LookupError):
        store.receive_mail("demo-mail-sales", example_mail())
    assert store.receive_mail("replacement", example_mail()).profile_id == "demo-p1"
    other = store.get_profile("demo-p2")
    with pytest.raises(BindingConflict):
        store.seed_profiles(((other, "demo-mail-sales", 10002),))


def test_unknown_mail_binding_is_rejected(store: Store):
    with pytest.raises(LookupError):
        store.receive_mail("unknown", example_mail())


def test_mail_replay_and_uid_validity(store: Store):
    mail = example_mail()
    first = store.receive_mail("demo-mail-sales", mail)
    again = store.receive_mail("demo-mail-sales", mail)
    second_profile = store.receive_mail("demo-mail-review", mail)
    second_generation = store.receive_mail("demo-mail-sales", example_mail(uidvalidity=43))
    assert not first.duplicate
    assert again.duplicate and again.event_id == first.event_id
    assert second_profile.profile_id == "demo-p2"
    assert len({first.event_id, second_profile.event_id, second_generation.event_id}) == 3
    assert store.get_mail("demo-p2", first.event_id) is None
    assert store.get_mail("demo-p1", first.event_id) == mail


def test_concurrent_mail_replay_has_one_winner(store: Store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: store.receive_mail("demo-mail-sales", example_mail()), range(2),
        ))
    assert results[0].event_id == results[1].event_id
    assert sorted(result.duplicate for result in results) == [False, True]


def test_draft_is_scoped_and_first_version_stays_stable(store: Store):
    event = store.receive_mail("demo-mail-sales", example_mail())
    candidate = MailCandidate(topic="delivery", requires_review=True, rationale="Обязательство")
    with pytest.raises(LookupError):
        store.record_draft("demo-p2", event.event_id, candidate)
    dispatch = MailDispatch(action="reply", target_ref="test-customer")
    version = store.record_draft("demo-p1", event.event_id, candidate, mail_dispatch=dispatch)
    request = ApprovalRequest(
        approval_id="a1", profile_id="demo-p1", document_id=version.document_id,
        version=version.version, actor_ref="leader",
    )
    card = ApprovalCardView(request=request, document=version)
    assert card.document.mail_dispatch == dispatch
    with pytest.raises(ValidationError):
        ApprovalCardView(request=request.model_copy(update={"version": 2}), document=version)
    changed = MailCandidate(topic="delivery", requires_review=True, rationale="Новый текст")
    assert store.record_draft("demo-p1", event.event_id, changed) == version
    reopened = Store(store.path)
    reopened.initialize()
    assert reopened.get_version("demo-p1", version.document_id, 1) == version
    assert reopened.get_version("demo-p2", version.document_id, 1) is None


def test_smoke_restarts_without_external_calls(store: Store):
    first = smoke(store)
    second = smoke(Store(store.path))
    assert first == second
    assert second["external_calls"] == 0
    assert second["other_profile_isolated"] is True


def test_mail_contract_rejects_invalid_timestamp_and_unknown_fields():
    payload = example_mail().model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        MailReceipt.model_validate({**payload, "received_at": "2026-12-07T07:00:00"})
    with pytest.raises(ValidationError):
        MailReceipt.model_validate({**payload, "profile_id": "caller-chosen"})


def test_fake_ports_record_decisions_and_failures():
    actor = TelegramActor(telegram_user_id=10001)
    decisions = FakeApprovals(DecisionStatus.STALE)
    assert decisions.decide("approval-1", actor, Decision.APPROVE) == DecisionStatus.STALE
    assert decisions.calls == [("approval-1", actor, Decision.APPROVE, None)]
    effect = Effect(
        effect_id="test", profile_id="demo-p1", key="k", kind=EffectKind.EMAIL, target_ref="sales",
    )
    transport = FakeTransport(EffectResult(status=DeliveryStatus.UNKNOWN, error_code="timeout"))
    assert transport.execute(effect).status == DeliveryStatus.UNKNOWN
    assert transport.calls == [effect]
    model = FakeModel(answer=TimeoutError("test"))
    with pytest.raises(TimeoutError):
        model.generate_json(MailCandidate, [{"role": "user", "content": "test"}])
    assert len(model.calls) == 1
