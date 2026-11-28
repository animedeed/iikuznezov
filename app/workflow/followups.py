"""Minutes pipeline glue and post-approval calendar scheduling.

ApprovalService.propose accepts only email and task deliveries, so calendar events are
queued here, in one write transaction that first checks the version was approved."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from json import dumps
from uuid import NAMESPACE_URL, uuid5

from app.calendar.ports import EventSpec
from app.contracts import (
    ApprovalCardView,
    Effect,
    EffectKind,
    MinutesCandidate,
)
from app.core.approvals import ApprovalService, EffectIntent, MeetingSource
from app.core.effects import enqueue_effect
from app.core.store import Store
from app.ports import ModelPort
from app.workflow.minutes import generate_minutes


class ApprovalRequired(ValueError):
    """Calendar work was requested for a version that is not approved."""


def _complete_tasks(candidate: MinutesCandidate, source: MeetingSource):
    allowed = set(source.allowed_assignee_refs)
    return [t for t in candidate.tasks
            if t.status == "draft" and t.due_date is not None and t.assignee_ref in allowed]


def minutes_intents(
    candidate: MinutesCandidate, source: MeetingSource, *,
    recipient_fingerprints: Mapping[str, str] | None = None,
) -> tuple[EffectIntent, ...]:
    """Email to each participant, one task per complete task. Mirrors the checks in
    ApprovalService so the proposal is never rejected for a rule we could have applied."""
    emails = tuple(
        EffectIntent(
            kind=EffectKind.EMAIL, target_ref=ref,
            recipient_fingerprint=recipient_fingerprints.get(ref) if recipient_fingerprints is not None else None,
        )
        for ref in candidate.participant_refs
        if recipient_fingerprints is None or ref in recipient_fingerprints
    )
    tasks = tuple(EffectIntent(kind=EffectKind.TASK, target_ref=t.task_ref)
                  for t in _complete_tasks(candidate, source))
    return emails + tasks


def load_meeting_source(store: Store, profile_id: str, event_id: str) -> tuple[MeetingSource, datetime]:
    with store._connection() as db:
        row = db.execute(
            "SELECT payload, received_at FROM inbox_events "
            "WHERE profile_id = ? AND event_id = ? AND source = 'meeting'",
            (profile_id, event_id),
        ).fetchone()
    if row is None:
        raise LookupError("Unknown meeting in this profile")
    return MeetingSource.model_validate_json(row["payload"]), datetime.fromisoformat(row["received_at"])


def process_meeting_event(
    store: Store, approvals: ApprovalService, model: ModelPort, profile_id: str, event_id: str,
    *, title: str | None = None, recipient_fingerprints: Mapping[str, str] | None = None,
) -> ApprovalCardView:
    """Generate minutes for a recorded transcript and open one approval for the initiator.
    Safe to repeat: an existing document skips the model and returns the current card."""
    source, received_at = load_meeting_source(store, profile_id, event_id)
    with store._connection() as db:
        existing = db.execute(
            "SELECT document_id FROM documents WHERE profile_id = ? AND source_event_id = ? AND kind = 'minutes'",
            (profile_id, event_id),
        ).fetchone()
    if existing is not None:
        saved = store.get_version(profile_id, existing["document_id"], 1)
        if saved is None:
            raise RuntimeError("Document has no original version")
        return approvals.propose(profile_id, event_id, saved.candidate)
    candidate = generate_minutes(
        model, source, approvals.list_preferences(profile_id),
        occurred_at=received_at.astimezone(UTC), title=title,
    )
    return approvals.propose(
        profile_id, event_id, candidate,
        intents=minutes_intents(candidate, source, recipient_fingerprints=recipient_fingerprints),
    )


def deadline_events(profile_id: str, document_id: str, version: int,
                    candidate: MinutesCandidate, allowed_assignees: Sequence[str]) -> tuple[EventSpec, ...]:
    """One all-day event on each complete task's due date."""
    events = []
    allowed = set(allowed_assignees)
    for task in candidate.tasks:
        if task.status != "draft" or task.due_date is None or task.assignee_ref not in allowed:
            continue
        uid = uuid5(NAMESPACE_URL, f"{profile_id}:{document_id}:{version}:{task.task_ref}").hex
        events.append(EventSpec(
            uid=f"{uid}@iikuznezov", summary=f"Срок: {task.action}"[:500],
            description=f"Исполнитель: {task.assignee_ref}. Из протокола «{candidate.title}».",
            start_date=task.due_date,
        ))
    return tuple(events)


def schedule_calendar_events(
    store: Store, profile_id: str, document_id: str, version: int, specs: Sequence[EventSpec],
) -> tuple[str, ...]:
    """Queue calendar writes for an approved version. Exact repeats are no-ops."""
    effect_ids: list[str] = []
    with store._connection(write=True) as db:
        approval = db.execute(
            """SELECT 1 FROM approvals AS a JOIN documents AS d USING (profile_id, document_id)
            WHERE a.profile_id = ? AND a.document_id = ? AND a.version = ?
              AND a.state = 'approved' AND d.kind = 'minutes'""",
            (profile_id, document_id, version),
        ).fetchone()
        if approval is None:
            raise ApprovalRequired("An approved minutes version is required")
        for spec in specs:
            key = dumps([document_id, version, EffectKind.CALENDAR_EVENT.value, spec.uid],
                        separators=(",", ":"), ensure_ascii=False)
            effect_id = uuid5(NAMESPACE_URL, f"{profile_id}:{key}").hex
            enqueue_effect(db, Effect(
                effect_id=effect_id, profile_id=profile_id, document_id=document_id, version=version,
                key=key, kind=EffectKind.CALENDAR_EVENT, target_ref=spec.uid,
                payload={"event": spec.model_dump(mode="json")},
            ))
            effect_ids.append(effect_id)
    return tuple(effect_ids)


def sweep_approved_minutes(
    store: Store, *, deadline_events_enabled: bool = True, profile_id: str | None = None,
) -> int:
    """Queue deadline events for every approved minutes version. Idempotent; call it from the
    executor loop before EffectQueue.run_once. Returns how many events were considered."""
    if not deadline_events_enabled:
        return 0
    with store._connection() as db:
        rows = db.execute(
            """SELECT a.profile_id, a.document_id, a.version, d.source_event_id
            FROM approvals AS a JOIN documents AS d
              ON d.profile_id = a.profile_id AND d.document_id = a.document_id
            WHERE a.state = 'approved' AND d.kind = 'minutes'
              AND (? IS NULL OR a.profile_id = ?) ORDER BY a.decided_at""",
            (profile_id, profile_id),
        ).fetchall()
    total = 0
    for row in rows:
        version = store.get_version(row["profile_id"], row["document_id"], row["version"])
        if version is None or not isinstance(version.candidate, MinutesCandidate):
            continue
        source, _ = load_meeting_source(store, row["profile_id"], row["source_event_id"])
        specs = deadline_events(row["profile_id"], row["document_id"], row["version"],
                                version.candidate, source.allowed_assignee_refs)
        try:
            schedule_calendar_events(store, row["profile_id"], row["document_id"], row["version"], specs)
        except ApprovalRequired:
            # A concurrent revision can supersede the approved version after the read above.
            continue
        total += len(specs)
    return total
