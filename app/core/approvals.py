"""Persistent approval coordinator. Bot callbacks can decide but cannot supply effects."""

from collections.abc import Sequence
from datetime import UTC, datetime
from json import dumps
from sqlite3 import Connection, Row
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from app.contracts import (
    ApprovalCardView,
    ApprovalRequest,
    Candidate,
    Contract,
    Decision,
    DecisionStatus,
    DocumentVersion,
    Effect,
    EffectKind,
    MailCandidate,
    MailDispatch,
    MeetingTurn,
    MinutesCandidate,
    Ref,
    ShortText,
    TelegramActor,
    UtcMoment,
)
from app.core.effects import enqueue_effect
from app.core.store import Store

_DECISION_STATES = {
    Decision.APPROVE: "approved",
    Decision.REJECT: "rejected",
    Decision.REVISE: "revision_requested",
}
_EFFECTS = TypeAdapter(list[Effect])


class TranscriptTurn(MeetingTurn):
    role: Literal["participant", "assistant"] = "participant"


class MeetingSource(Contract):
    meeting_id: Ref
    initiator: TelegramActor
    participant_refs: tuple[Ref, ...]
    allowed_assignee_refs: tuple[Ref, ...] = ()
    turns: tuple[TranscriptTurn, ...]


class ApprovalDecisionRecord(Contract):
    approval_id: Ref
    profile_id: Ref
    document_id: Ref
    version: int
    decision: Decision
    decided_by: int
    decided_at: UtcMoment
    comment: str | None


class EffectIntent(Contract):
    """A delivery prepared by trusted module code, never copied from a callback."""

    kind: EffectKind
    target_ref: Ref


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _audit(
    db: Connection, approval: Row, event: str, timestamp: str, *,
    actor: int | None = None, decision: Decision | None = None,
) -> None:
    db.execute(
        """INSERT INTO approval_audit
        (profile_id, document_id, version, approval_id, event, actor_telegram_id, decision, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (approval["profile_id"], approval["document_id"], approval["version"], approval["approval_id"],
         event, actor, decision, timestamp),
    )


class ApprovalService:
    def __init__(self, store: Store):
        self.store = store

    def submit_transcript(self, profile_id: str, source: MeetingSource) -> str:
        """Register input from an already authorized meeting session."""
        with self.store._connection(write=True) as db:
            event_id = uuid4().hex
            inserted = db.execute(
                """INSERT INTO inbox_events (profile_id, event_id, source, external_key, payload, received_at)
                VALUES (?, ?, 'meeting', ?, ?, ?)
                ON CONFLICT (profile_id, source, external_key) DO NOTHING RETURNING event_id""",
                (profile_id, event_id, source.meeting_id, source.model_dump_json(), _now()),
            ).fetchone()
            if inserted is not None:
                return event_id
            previous = db.execute(
                """SELECT event_id, payload FROM inbox_events
                WHERE profile_id = ? AND source = 'meeting' AND external_key = ?""",
                (profile_id, source.meeting_id),
            ).fetchone()
            if MeetingSource.model_validate_json(previous["payload"]) != source:
                raise ValueError("Meeting already has a different final transcript")
            return previous["event_id"]

    def propose(
        self, profile_id: str, source_id: str, candidate: Candidate, *,
        mail_dispatch: MailDispatch | None = None, intents: Sequence[EffectIntent] = (),
        mail_channel_id: str | None = None,
    ) -> ApprovalCardView:
        """Open or read the current approval for a recorded source."""
        with self.store._connection(write=True) as db:
            if mail_channel_id is not None:
                active = db.execute(
                    """SELECT 1 FROM bindings AS b JOIN inbox_events AS e ON b.profile_id=e.profile_id
                       WHERE b.kind='mail' AND b.active=1 AND b.profile_id=? AND b.channel_id=?
                         AND e.event_id=? AND e.source='mail'
                         AND json_extract(e.external_key, '$[0]')=b.channel_id""",
                    (profile_id, mail_channel_id, source_id),
                ).fetchone()
                if active is None:
                    raise PermissionError("Mail channel binding changed during intake")
            if isinstance(candidate, MailCandidate):
                automatic = db.execute(
                    """SELECT 1 FROM inbox_events WHERE profile_id=? AND event_id=?
                       AND source='mail' AND status='mail_auto_forward_queued'""",
                    (profile_id, source_id),
                ).fetchone()
                if automatic is not None:
                    raise ValueError("Mail source already has an automatic delivery queued")
            first = self.store._record_draft(db, profile_id, source_id, candidate, mail_dispatch)
            current = self.store._get_version(db, profile_id, first.document_id)
            if current is None:
                raise RuntimeError("Document version missing")
            previous = self._find_for_version(db, current)
            if previous is not None:
                if previous["state"] != "pending_approval":
                    raise ValueError("Current approval is closed")
                return self._view(current, previous)
            if current.candidate != candidate or current.mail_dispatch != mail_dispatch:
                raise ValueError("Proposal does not match the stored draft")
            view = self._open(db, current, intents)
            db.execute(
                "UPDATE inbox_events SET status = 'processed' WHERE profile_id = ? AND event_id = ?",
                (profile_id, source_id),
            )
            return view

    def get_pending(self, profile_id: str, approval_id: str, actor: TelegramActor) -> ApprovalCardView | None:
        with self.store._connection() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE profile_id = ? AND approval_id = ? AND state = 'pending_approval'",
                (profile_id, approval_id),
            ).fetchone()
            if row is None or not self._authorized(db, row, actor):
                return None
            version = self.store._get_version(db, profile_id, row["document_id"])
            return self._view(version, row) if version is not None and version.version == row["version"] else None

    def get_decision(
        self, profile_id: str, approval_id: str, actor: TelegramActor,
    ) -> ApprovalDecisionRecord | None:
        with self.store._connection() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE profile_id = ? AND approval_id = ? AND decision IS NOT NULL",
                (profile_id, approval_id),
            ).fetchone()
            if row is None or not self._authorized(db, row, actor):
                return None
            return ApprovalDecisionRecord(
                approval_id=row["approval_id"], profile_id=row["profile_id"], document_id=row["document_id"],
                version=row["version"], decision=row["decision"], decided_by=row["decided_by"],
                decided_at=row["decided_at"], comment=row["comment"],
            )

    def decide(
        self, approval_id: str, actor: TelegramActor, decision: Decision, comment: str | None = None,
    ) -> DecisionStatus:
        if not isinstance(decision, Decision) or (comment is not None and (
            not isinstance(comment, str) or len(comment) > 2_000
        )):
            return DecisionStatus.VALIDATION_ERROR
        with self.store._connection(write=True) as db:
            row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or not self._authorized(db, row, actor):
                return DecisionStatus.FORBIDDEN
            version = self.store._get_version(db, row["profile_id"], row["document_id"])
            if version is None or version.version != row["version"] or row["state"] == "superseded":
                return DecisionStatus.STALE
            if row["state"] != "pending_approval":
                return DecisionStatus.ALREADY_DECIDED
            at = _now()
            updated = db.execute(
                """UPDATE approvals SET state = ?, decision = ?, decided_by = ?, decided_at = ?, comment = ?
                WHERE approval_id = ? AND state = 'pending_approval'""",
                (_DECISION_STATES[decision], decision, actor.telegram_user_id, at, comment, approval_id),
            )
            if updated.rowcount != 1:
                return DecisionStatus.ALREADY_DECIDED
            _audit(db, row, "decided", at, actor=actor.telegram_user_id, decision=decision)
            self._cancel_notices(db, row, at)
            if decision == Decision.APPROVE:
                for effect in _EFFECTS.validate_json(row["proposed_effects"]):
                    enqueue_effect(db, effect)
            return DecisionStatus.ACCEPTED

    def revise_document(
        self, profile_id: str, document_id: str, actor: TelegramActor, expected_version: int,
        candidate: Candidate, *, mail_dispatch: MailDispatch | None = None, intents: Sequence[EffectIntent] = (),
    ) -> ApprovalCardView:
        """Save a server-validated replacement snapshot and invalidate the previous card."""
        with self.store._connection(write=True) as db:
            latest = self.store._get_version(db, profile_id, document_id)
            if latest is None:
                raise LookupError("Unknown document in this profile")
            previous = self._find_for_version(db, latest)
            if previous is None or not self._authorized(db, previous, actor):
                raise PermissionError("User cannot revise this approval")
            if latest.version != expected_version:
                raise ValueError("Stale document version")
            if latest.candidate.kind != candidate.kind:
                raise ValueError("A revision cannot change the document kind")
            started = db.execute(
                """SELECT 1 FROM effects WHERE profile_id = ? AND document_id = ?
                AND version = ? AND kind <> 'telegram' AND attempts > 0 LIMIT 1""",
                (profile_id, document_id, expected_version),
            ).fetchone()
            if started is not None:
                raise ValueError("Delivery already started; open a separate correction")
            db.execute(
                """UPDATE effects SET status = 'cancelled' WHERE profile_id = ? AND document_id = ?
                AND version = ? AND status = 'pending' AND attempts = 0""",
                (profile_id, document_id, expected_version),
            )
            db.execute("UPDATE approvals SET state = 'superseded' WHERE approval_id = ?", (previous["approval_id"],))
            at = _now()
            _audit(db, previous, "superseded", at, actor=actor.telegram_user_id)
            self._cancel_notices(db, previous, at)
            next_version = DocumentVersion(
                profile_id=profile_id, source_id=latest.source_id, document_id=document_id,
                version=expected_version + 1, candidate=candidate, mail_dispatch=mail_dispatch,
            )
            self.store._insert_version(db, next_version)
            return self._open(db, next_version, intents)

    def save_preference(self, profile_id: str, actor: TelegramActor, text: str, source_ref: str) -> str:
        value = TypeAdapter(ShortText).validate_python(text)
        with self.store._connection(write=True) as db:
            if self._leader_id(db, profile_id) != actor.telegram_user_id:
                raise PermissionError("Only this profile's leader can save a preference")
            if self.store._get_version(db, profile_id, source_ref) is None:
                raise LookupError("Unknown source in this profile")
            preference_id = uuid4().hex
            db.execute(
                """INSERT INTO preferences VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (profile_id, source_document_id, text) DO NOTHING""",
                (preference_id, profile_id, source_ref, actor.telegram_user_id, value, _now()),
            )
            result = db.execute(
                "SELECT preference_id FROM preferences WHERE profile_id = ? AND source_document_id = ? AND text = ?",
                (profile_id, source_ref, value),
            ).fetchone()
            return result["preference_id"]

    def list_preferences(self, profile_id: str) -> tuple[str, ...]:
        """Read one trusted profile's preferences for draft generation."""
        with self.store._connection() as db:
            return tuple(row["text"] for row in db.execute(
                "SELECT text FROM preferences WHERE profile_id = ? ORDER BY created_at, preference_id",
                (profile_id,),
            ))

    @staticmethod
    def _cancel_notices(db: Connection, approval: Row, at: str) -> None:
        db.execute(
            """UPDATE effects SET status = 'cancelled', updated_at = ?
            WHERE profile_id = ? AND kind = 'telegram' AND target_ref = ?
                AND status IN ('pending', 'retryable')""",
            (at, approval["profile_id"], approval["approval_id"]),
        )

    @staticmethod
    def _leader_id(db: Connection, profile_id: str) -> int | None:
        bindings = db.execute(
            "SELECT channel_id FROM bindings WHERE profile_id = ? AND kind = 'telegram' AND active = 1",
            (profile_id,),
        ).fetchall()
        return int(bindings[0]["channel_id"]) if len(bindings) == 1 else None

    def _authorized(self, db: Connection, row: Row, actor: TelegramActor) -> bool:
        if row["approver_telegram_id"] != actor.telegram_user_id:
            return False
        return row["approver_ref"] != "leader" or self._leader_id(db, row["profile_id"]) == actor.telegram_user_id

    @staticmethod
    def _find_for_version(db: Connection, version: DocumentVersion) -> Row | None:
        return db.execute(
            "SELECT * FROM approvals WHERE profile_id = ? AND document_id = ? AND version = ?",
            (version.profile_id, version.document_id, version.version),
        ).fetchone()

    @staticmethod
    def _view(version: DocumentVersion, row: Row) -> ApprovalCardView:
        return ApprovalCardView(
            request=ApprovalRequest(
                approval_id=row["approval_id"], profile_id=version.profile_id,
                document_id=version.document_id, version=version.version, actor_ref=row["approver_ref"],
            ),
            document=version,
        )

    def _open(
        self, db: Connection, version: DocumentVersion, intents: Sequence[EffectIntent],
    ) -> ApprovalCardView:
        actor_ref = "leader"
        approver_id = self._leader_id(db, version.profile_id)
        meeting = None
        if isinstance(version.candidate, MinutesCandidate):
            source = db.execute(
                """SELECT payload FROM inbox_events
                WHERE profile_id = ? AND event_id = ? AND source = 'meeting'""",
                (version.profile_id, version.source_id),
            ).fetchone()
            if source is None:
                raise LookupError("Meeting source missing")
            meeting = MeetingSource.model_validate_json(source["payload"])
            if (version.candidate.meeting_id != meeting.meeting_id
                    or not set(version.candidate.participant_refs).issubset(meeting.participant_refs)):
                raise ValueError("Minutes refer to another meeting or unrecognized participants")
            actor_ref = "meeting-initiator"
            approver_id = meeting.initiator.telegram_user_id
        if approver_id is None:
            raise LookupError("Approval has no trusted recipient")
        effects = self._prepare_effects(version, intents, meeting)
        at = _now()
        row = db.execute(
            """INSERT INTO approvals
            (approval_id, profile_id, document_id, version, approver_ref, approver_telegram_id,
             state, proposed_effects, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?) RETURNING *""",
            (uuid4().hex, version.profile_id, version.document_id, version.version,
             actor_ref, approver_id, _EFFECTS.dump_json(effects).decode(), at),
        ).fetchone()
        _audit(db, row, "opened", at)
        enqueue_effect(db, Effect(
            effect_id=uuid4().hex, profile_id=version.profile_id, document_id=version.document_id,
            version=version.version, kind=EffectKind.TELEGRAM,
            key=f"approval-notice:{row['approval_id']}", target_ref=row["approval_id"],
            payload={"operation": "approval_notice", "telegram_user_id": approver_id},
        ))
        return self._view(version, row)

    @staticmethod
    def _prepare_effects(
        version: DocumentVersion, intents: Sequence[EffectIntent], meeting: MeetingSource | None,
    ) -> list[Effect]:
        effects = []
        seen: set[tuple[EffectKind, str]] = set()
        completed_tasks = {
            task.task_ref: task for task in version.candidate.tasks
            if meeting is not None and task.assignee_ref in meeting.allowed_assignee_refs
            and task.due_date is not None and task.status == "draft"
        } if isinstance(version.candidate, MinutesCandidate) else {}
        for intent in intents:
            key = (intent.kind, intent.target_ref)
            if key in seen:
                raise ValueError("Duplicate delivery in one approval")
            seen.add(key)
            if isinstance(version.candidate, MailCandidate):
                dispatch = version.mail_dispatch
                if (dispatch is None or intent.target_ref != dispatch.target_ref
                        or dispatch.action not in {"forward", "reply"} or intent.kind != EffectKind.EMAIL
                        or (dispatch.action == "reply" and not version.candidate.draft_text)):
                    raise ValueError("Delivery does not match the stored mail action")
                payload = {
                    "action": dispatch.action, "source_id": version.source_id,
                    "draft_text": version.candidate.draft_text,
                }
            elif intent.kind == EffectKind.EMAIL:
                if intent.target_ref not in version.candidate.participant_refs:
                    raise ValueError("Minutes delivery target is outside the approved version")
                payload = {"minutes": version.candidate.model_dump(mode="json")}
            elif intent.kind == EffectKind.TASK:
                task = completed_tasks.get(intent.target_ref)
                if task is None:
                    raise ValueError("Delivery has no complete approved task")
                payload = {"task": task.model_dump(mode="json")}
            else:
                raise ValueError("Unexpected protocol delivery")
            effects.append(Effect(
                effect_id=uuid4().hex, profile_id=version.profile_id, document_id=version.document_id,
                version=version.version, kind=intent.kind, target_ref=intent.target_ref, payload=payload,
                key=dumps([version.document_id, version.version, intent.kind, intent.target_ref],
                          separators=(",", ":"), ensure_ascii=False),
            ))
        return effects
