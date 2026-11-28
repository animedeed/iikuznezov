import pytest

from app.contracts import MailCandidate, MailReceipt, PolicyClass, Profile, Route, TelegramActor, Decision
from app.core.approvals import ApprovalService
from app.core.mail_intake import process_mail
from app.core.store import Store


def receipt(uid=101):
    return MailReceipt.model_validate({
        "folder": "INBOX", "uidvalidity": 42, "uid": uid,
        "from": "customer@example.invalid", "subject": "Запрос цены",
        "text": "Пришлите цену на товар.", "received_at": "2026-11-28T07:00:00Z",
    })


def candidate(**changes):
    values = dict(topic="quote_request", requires_review=False,
                  rationale="Запрос цены по товару.", draft_text="См. предложение.")
    values.update(changes)
    return MailCandidate(**values)


def setup(tmp_path, route=Route.FORWARD_SALES, channel="mail-a"):
    store = Store(tmp_path / "mail.sqlite3")
    store.initialize()
    store.seed_profiles(((Profile(profile_id="p1", timezone="UTC", quote_route=route), channel, 123),))
    return store


def test_m001_review_opens_approval_without_business_effect_before_confirmation(tmp_path):
    store = setup(tmp_path, Route.REVIEW)
    result = process_mail(
        store, "mail-a", receipt(), candidate(topic="delivery_commitment", requires_review=True,
            policy_class=PolicyClass.CUSTOMER_COMMITMENT), sales_alias="sales-desk",
    )
    assert result.status == "approval_pending"
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE state='pending_approval'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM effects WHERE kind='email'").fetchone()[0] == 0
    # Core approval ACL decides the proposal, and only then creates a business effect.
    assert ApprovalService(store).decide(
        result.approval_id, TelegramActor(telegram_user_id=123), Decision.APPROVE,
    ).value == "accepted"
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM effects WHERE kind='email'").fetchone()[0] == 1


def test_m002_exact_allowlisted_case_enqueues_one_stable_effect_on_replay(tmp_path):
    store = setup(tmp_path)
    first = process_mail(store, "mail-a", receipt(), candidate(), sales_alias="sales-desk", trusted_quote=True)
    second = process_mail(store, "mail-a", receipt(), candidate(), sales_alias="sales-desk")
    assert first.status == second.status == "auto_forward_queued"
    with pytest.raises(ValueError, match="already has an automatic delivery"):
        ApprovalService(store).propose("p1", first.event_id, candidate())
    assert second.duplicate is True
    assert first.effect_id == second.effect_id
    with store._connection() as db:
        rows = db.execute("SELECT effect_id, key, status, attempts FROM effects WHERE kind='email'").fetchall()
        assert len(rows) == 1
        assert rows[0]["effect_id"] == first.effect_id
        assert rows[0]["status"] == "pending" and rows[0]["attempts"] == 0


def test_review_route_policy_flag_and_missing_recipient_never_auto_forward(tmp_path):
    untrusted_case = setup(tmp_path / "untrusted")
    result = process_mail(untrusted_case, "mail-a", receipt(), candidate(), sales_alias="sales-desk")
    assert result.status == "approval_pending"

    cases = [
        (Route.REVIEW, candidate(), "sales-desk"),
        (Route.FORWARD_SALES, candidate(requires_review=True), "sales-desk"),
        (Route.FORWARD_SALES, candidate(policy_class=PolicyClass.FINANCE), "sales-desk"),
        (Route.FORWARD_SALES, candidate(), None),
        (Route.FORWARD_SALES, candidate(topic="Quote_Request"), "sales-desk"),
    ]
    for index, (route, mail, alias) in enumerate(cases):
        store = setup(tmp_path / str(index), route=route, channel=f"mail-{index}")
        result = process_mail(store, f"mail-{index}", receipt(index + 200), mail, sales_alias=alias, trusted_quote=True)
        assert result.status == "approval_pending"
        with store._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM effects WHERE kind='email'").fetchone()[0] == 0


def test_untrusted_or_inactive_channel_is_rejected(tmp_path):
    store = setup(tmp_path)
    try:
        process_mail(store, "unknown", receipt(), candidate(), sales_alias="sales-desk")
    except LookupError:
        pass
    else:
        raise AssertionError("untrusted mail channel was accepted")
