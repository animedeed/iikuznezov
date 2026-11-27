"""Persistent, idempotent delivery queue. Transport calls happen outside DB transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps, loads
from sqlite3 import Connection
from time import time
from uuid import uuid4

from app.contracts import DeliveryStatus, Effect, EffectResult
from app.core.store import Store
from app.ports import EffectTransport

_MAX_ATTEMPTS = 4


class EffectConflict(ValueError):
    """An effect idempotency key was reused with different immutable data."""


class EffectStateError(ValueError):
    """The requested queue transition is not safe from the current state."""


@dataclass(frozen=True)
class EffectStatus:
    effect_id: str
    profile_id: str
    document_id: str | None
    version: int | None
    status: str
    attempts: int
    external_id: str | None
    error_code: str | None
    created_at: str
    updated_at: str


def _canonical_payload(effect: Effect) -> str:
    return dumps(effect.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _immutable(effect: Effect) -> tuple[object, ...]:
    return (
        effect.effect_id, effect.profile_id, effect.document_id, effect.version,
        effect.key, effect.kind.value, effect.target_ref, _canonical_payload(effect),
    )


def enqueue_effect(db: Connection, effect: Effect) -> None:
    """Insert an effect into the caller's transaction, accepting exact retries only."""
    prior = db.execute(
        "SELECT effect_id, profile_id, document_id, version, key, kind, target_ref, payload "
        "FROM effects WHERE effect_id = ? OR (profile_id = ? AND key = ?)",
        (effect.effect_id, effect.profile_id, effect.key),
    ).fetchall()
    if prior:
        expected = _immutable(effect)
        if any(tuple(row) != expected for row in prior) or len(prior) != 1:
            raise EffectConflict("Effect id or key already exists with different data")
        return
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO effects
        (effect_id, profile_id, document_id, version, key, kind, target_ref, payload,
         status, attempts, next_attempt_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)""",
        (*_immutable(effect), now, now),
    )


class EffectQueue:
    def __init__(self, store: Store):
        self.store = store

    def status(self, effect_id: str) -> EffectStatus | None:
        with self.store._connection() as db:
            row = db.execute(
                """SELECT effect_id, profile_id, document_id, version, status, attempts,
                external_id, error_code, created_at, updated_at FROM effects WHERE effect_id = ?""",
                (effect_id,),
            ).fetchone()
            return EffectStatus(**dict(row)) if row is not None else None

    def _expire_leases(self, now: float) -> None:
        stamp = datetime.now(UTC).isoformat()
        with self.store._connection(write=True) as db:
            db.execute(
                """UPDATE effects SET status = 'unknown', lease_token = NULL,
                lease_expires_at = NULL, error_code = 'lease_expired', updated_at = ?
                WHERE status = 'sending' AND lease_expires_at <= ?""",
                (stamp, now),
            )

    def run_once(self, transport: EffectTransport, *, lease_seconds: int = 60) -> str | None:
        """Claim and process one effect. Ambiguous transport exceptions become unknown."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_epoch = time()
        self._expire_leases(now_epoch)
        token = uuid4().hex
        stamp = datetime.now(UTC).isoformat()
        with self.store._connection(write=True) as db:
            # A notice can finish in retryable after its card was closed by another actor.
            db.execute(
                """UPDATE effects AS e SET status = 'cancelled', updated_at = ?
                WHERE e.status IN ('pending', 'retryable') AND e.kind = 'telegram'
                    AND json_extract(e.payload, '$.operation') = 'approval_notice'
                    AND NOT EXISTS (
                        SELECT 1 FROM approvals AS a WHERE a.approval_id = e.target_ref
                        AND a.profile_id = e.profile_id AND a.state = 'pending_approval'
                        AND (a.approver_ref = 'meeting-initiator' OR EXISTS (
                            SELECT 1 FROM bindings AS b WHERE b.profile_id = e.profile_id
                            AND b.kind = 'telegram' AND b.active = 1
                            AND b.channel_id = CAST(a.approver_telegram_id AS TEXT)
                        ))
                    )""",
                (stamp,),
            )
            row = db.execute(
                """SELECT * FROM effects WHERE status IN ('pending', 'retryable')
                AND next_attempt_at <= ? ORDER BY created_at, effect_id LIMIT 1""",
                (now_epoch,),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                """UPDATE effects SET status='sending', attempts=attempts+1, lease_token=?,
                lease_expires_at=?, updated_at=? WHERE effect_id=? AND status IN ('pending','retryable')""",
                (token, now_epoch + lease_seconds, stamp, row["effect_id"]),
            ).rowcount
            if not changed:
                return None
            attempts = row["attempts"] + 1
            effect = Effect(
                effect_id=row["effect_id"], profile_id=row["profile_id"],
                document_id=row["document_id"], version=row["version"], key=row["key"],
                kind=row["kind"], target_ref=row["target_ref"], payload=loads(row["payload"]),
            )

        try:
            result = transport.execute(effect)
            if not isinstance(result, EffectResult):
                raise TypeError("EffectTransport.execute must return EffectResult")
        except Exception:
            result = EffectResult(status=DeliveryStatus.UNKNOWN, error_code="transport_exception")

        status = result.status.value
        updated = datetime.now(UTC).isoformat()
        delay = 0
        error_code = result.error_code
        if status == "retryable":
            if attempts >= _MAX_ATTEMPTS:
                status = "failed"
                error_code = "retry_limit_reached"
            else:
                delay = max(2 ** attempts, result.retry_after_seconds or 0)
        with self.store._connection(write=True) as db:
            db.execute(
                """UPDATE effects SET status=?, external_id=?, error_code=?,
                lease_token=NULL, lease_expires_at=NULL, next_attempt_at=?, updated_at=?
                WHERE effect_id=? AND status='sending' AND lease_token=?""",
                (status, result.external_id, error_code, time() + delay, updated, effect.effect_id, token),
            )
        return effect.effect_id

    def retry(self, effect_id: str, *, verified_by: str | None = None) -> None:
        """Requeue retryable work; unknown work needs explicit operator confirmation."""
        stamp = datetime.now(UTC).isoformat()
        with self.store._connection(write=True) as db:
            row = db.execute("SELECT status FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise LookupError("Unknown effect")
            state = row["status"]
            if state == "unknown":
                if verified_by is None or not 1 <= len(verified_by.strip()) <= 128:
                    raise EffectStateError("Record who verified no delivery before retrying an unknown outcome")
                db.execute(
                    "INSERT INTO effect_retry_audit (effect_id, verified_by, verified_at) VALUES (?, ?, ?)",
                    (effect_id, verified_by.strip(), stamp),
                )
            if state not in {"retryable", "unknown"}:
                raise EffectStateError(f"Cannot retry effect in {state} state")
            db.execute(
                """UPDATE effects SET status='pending', next_attempt_at=0, error_code=NULL,
                external_id=NULL, updated_at=? WHERE effect_id=?""",
                (stamp, effect_id),
            )
