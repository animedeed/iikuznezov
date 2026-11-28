"""Offline mail policy entry point joining a trusted receipt to the foundation."""

from __future__ import annotations

from dataclasses import dataclass
from json import dumps
from uuid import NAMESPACE_URL, uuid5

from app.contracts import Effect, EffectKind, MailCandidate, MailDispatch, MailReceipt, Route
from app.core.approvals import ApprovalService, EffectIntent
from app.core.effects import enqueue_effect
from app.core.store import Store


@dataclass(frozen=True)
class MailIntakeResult:
    event_id: str
    profile_id: str
    status: str
    duplicate: bool
    document_id: str | None = None
    version: int | None = None
    approval_id: str | None = None
    effect_id: str | None = None


def process_mail(
    store: Store,
    channel_id: str,
    receipt: MailReceipt,
    candidate: MailCandidate,
    *,
    sales_alias: str | None,
    trusted_quote: bool = False,
    recipient_fingerprint: str | None = None,
) -> MailIntakeResult:
    """Persist a classified mail action; only exact quote requests can auto-forward.

    `channel_id` and `sales_alias` must come from trusted application configuration.
    `trusted_quote` requires independent confirmation from a trusted policy source.
    The function never performs network or provider I/O. Manual cases create an
    approval proposal; its business effect remains absent until approval.
    """
    received = store.receive_mail(channel_id, receipt)
    with store._connection() as db:
        binding = db.execute(
            """SELECT b.profile_id, p.quote_route, e.status FROM bindings b
            JOIN profiles p USING (profile_id)
            JOIN inbox_events e ON e.profile_id=b.profile_id AND e.event_id=?
            WHERE b.kind='mail' AND b.channel_id=? AND b.active=1 AND e.source='mail'""",
            (received.event_id, channel_id),
        ).fetchone()
    if binding is None or binding["profile_id"] != received.profile_id:
        raise PermissionError("Mail channel binding changed during intake")
    profile_id = binding["profile_id"]
    existing = _existing_result(store, profile_id, received.event_id, received.duplicate)
    if existing is not None:
        return existing

    route = Route(binding["quote_route"])
    automatic = (
        trusted_quote is True
        and candidate.topic == "quote_request"
        and route == Route.FORWARD_SALES
        and not candidate.requires_review
        and candidate.policy_class is None
        and bool(sales_alias)
    )
    if automatic:
        return _auto_forward(store, profile_id, received.event_id, candidate,
                             sales_alias, received.duplicate, channel_id, recipient_fingerprint)

    dispatch = MailDispatch(action="forward", target_ref=sales_alias) if sales_alias else None
    intents = (EffectIntent(
        kind=EffectKind.EMAIL, target_ref=sales_alias, recipient_fingerprint=recipient_fingerprint,
    ),) if sales_alias else ()
    card = ApprovalService(store).propose(
        profile_id, received.event_id, candidate, mail_dispatch=dispatch, intents=intents,
        mail_channel_id=channel_id,
    )
    with store._connection(write=True) as db:
        db.execute(
            "UPDATE inbox_events SET status='mail_approval_pending' "
            "WHERE profile_id=? AND event_id=? AND status='processed'",
            (profile_id, received.event_id),
        )
    return MailIntakeResult(
        event_id=received.event_id, profile_id=profile_id, status="approval_pending",
        duplicate=received.duplicate, document_id=card.document.document_id,
        version=card.document.version, approval_id=card.request.approval_id,
    )


def _auto_forward(
    store: Store, profile_id: str, event_id: str, candidate: MailCandidate,
    sales_alias: str, duplicate: bool, channel_id: str, recipient_fingerprint: str | None,
) -> MailIntakeResult:
    dispatch = MailDispatch(action="forward", target_ref=sales_alias)
    stable_key = dumps(["mail-auto-forward", profile_id, event_id, sales_alias], separators=(",", ":"))
    effect_id = uuid5(NAMESPACE_URL, stable_key).hex
    with store._connection(write=True) as db:
        event = db.execute(
            """SELECT e.status, p.quote_route FROM inbox_events e JOIN profiles p USING(profile_id)
            WHERE e.profile_id=? AND e.event_id=? AND e.source='mail'""",
            (profile_id, event_id),
        ).fetchone()
        if event is None:
            raise LookupError("Unknown mail event in profile")
        if event["status"] != "received":
            existing = _existing_result_db(db, profile_id, event_id, duplicate)
            if existing is not None:
                return existing
            raise ValueError("Mail event is already being processed")
        if Route(event["quote_route"]) != Route.FORWARD_SALES:
            raise PermissionError("Profile route changed during intake")
        active = db.execute(
            "SELECT 1 FROM bindings WHERE kind='mail' AND channel_id=? AND profile_id=? AND active=1",
            (channel_id, profile_id),
        ).fetchone()
        if active is None:
            raise PermissionError("Mail channel binding changed during intake")
        version = store._record_draft(db, profile_id, event_id, candidate, dispatch)
        if version.candidate != candidate or version.mail_dispatch != dispatch:
            raise ValueError("Mail event already has a different stored classification")
        effect = Effect(
            effect_id=effect_id, profile_id=profile_id, document_id=version.document_id,
            version=version.version, kind=EffectKind.EMAIL, key=stable_key,
            target_ref=sales_alias,
            payload={"action": "forward", "source_id": event_id,
                     "draft_text": candidate.draft_text,
                     **({"recipient_fingerprint": recipient_fingerprint} if recipient_fingerprint else {})},
        )
        enqueue_effect(db, effect)
        db.execute(
            "UPDATE inbox_events SET status='mail_auto_forward_queued' "
            "WHERE profile_id=? AND event_id=?",
            (profile_id, event_id),
        )
    return MailIntakeResult(
        event_id=event_id, profile_id=profile_id, status="auto_forward_queued", duplicate=duplicate,
        document_id=version.document_id, version=version.version, effect_id=effect_id,
    )


def _existing_result(
    store: Store, profile_id: str, event_id: str, duplicate: bool,
) -> MailIntakeResult | None:
    with store._connection() as db:
        return _existing_result_db(db, profile_id, event_id, duplicate)


def _existing_result_db(db, profile_id: str, event_id: str, duplicate: bool) -> MailIntakeResult | None:
    event = db.execute(
        "SELECT status FROM inbox_events WHERE profile_id=? AND event_id=? AND source='mail'",
        (profile_id, event_id),
    ).fetchone()
    if event is None or event["status"] == "received":
        return None
    row = db.execute(
        """SELECT d.document_id, v.version FROM documents d JOIN document_versions v
        USING(profile_id, document_id) WHERE d.profile_id=? AND d.source_event_id=? AND d.kind='mail_action'
        ORDER BY v.version DESC LIMIT 1""",
        (profile_id, event_id),
    ).fetchone()
    if row is None:
        return MailIntakeResult(event_id, profile_id, event["status"], duplicate)
    approval = db.execute(
        "SELECT approval_id, state FROM approvals WHERE profile_id=? AND document_id=? AND version=?",
        (profile_id, row["document_id"], row["version"]),
    ).fetchone()
    effect = db.execute(
        """SELECT effect_id FROM effects WHERE profile_id=? AND document_id=? AND version=?
        AND kind='email' ORDER BY created_at LIMIT 1""",
        (profile_id, row["document_id"], row["version"]),
    ).fetchone()
    if event["status"] == "mail_auto_forward_queued":
        status = "auto_forward_queued"
    elif approval is not None:
        status = "approval_pending" if approval["state"] == "pending_approval" else approval["state"]
    else:
        status = event["status"]
    return MailIntakeResult(
        event_id, profile_id, status, duplicate, row["document_id"], row["version"],
        approval["approval_id"] if approval is not None else None,
        effect["effect_id"] if effect is not None else None,
    )
