from app.contracts import MailCandidate, MailReceipt, PolicyClass, Profile, Route
from app.core.store import Store
from app.testing import FakeModel


def seed_demo(store: Store) -> None:
    store.seed_profiles((
        (Profile(profile_id="demo-p1", timezone="Europe/Moscow", quote_route=Route.FORWARD_SALES),
         "demo-mail-sales", 10001),
        (Profile(profile_id="demo-p2", timezone="Europe/Moscow", quote_route=Route.REVIEW),
         "demo-mail-review", 10002),
    ))


def smoke(store: Store) -> dict[str, object]:
    seed_demo(store)
    receipt = MailReceipt.model_validate({
        "folder": "INBOX", "uidvalidity": 42, "uid": 101,
        "from": "customer@example.invalid", "subject": "Подтверждение срока",
        "text": "Можете гарантировать поставку в пятницу?",
        "received_at": "2026-12-07T07:00:00Z",
    })
    incoming = store.receive_mail("demo-mail-sales", receipt)
    candidate = FakeModel(answer=MailCandidate(
        topic="delivery_commitment", requires_review=True,
        policy_class=PolicyClass.CUSTOMER_COMMITMENT,
        rationale="В письме запрашивают подтверждение обязательства.",
    )).generate_json(MailCandidate, [{"role": "user", "content": receipt.text}])
    version = store.record_draft(incoming.profile_id, incoming.event_id, candidate)
    duplicate = store.receive_mail("demo-mail-sales", receipt)
    if duplicate.event_id != incoming.event_id or not duplicate.duplicate:
        raise RuntimeError("Smoke did not deduplicate mail")
    if store.get_version("demo-p2", version.document_id, version.version) is not None:
        raise RuntimeError("Smoke exposed another profile's draft")
    return {
        "profile_id": incoming.profile_id,
        "event_id": incoming.event_id,
        "document_id": version.document_id,
        "version": version.version,
        "duplicate_detected": duplicate.duplicate,
        "other_profile_isolated": True,
        "external_calls": 0,
    }
