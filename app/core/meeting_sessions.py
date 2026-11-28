"""Offline-capable, temporary meeting sessions and their HTTP contract."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.contracts import MessageText, Ref, TelegramActor
from app.core.approvals import ApprovalService, MeetingSource, TranscriptTurn
from app.core.store import Store


@dataclass(frozen=True)
class MeetingContext:
    profile_id: str
    meeting_id: str
    sequence: int
    previous_context: str | None
    transcript: tuple[dict[str, str], ...]
    participant_text: str | None


Reply = Callable[[MeetingContext], str]


def _client_request_id(value: str) -> str:
    if value.startswith("assistant:"):
        raise ValueError("Request ID uses a reserved prefix")
    return value


ClientRequestId = Annotated[Ref, AfterValidator(_client_request_id)]


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ClientRequestId


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ClientRequestId
    sequence: int = Field(gt=0)
    text: MessageText
    role: Literal["participant"] = "participant"


class FinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ClientRequestId


class MeetingConflict(ValueError):
    pass


class MeetingUnauthorized(ValueError):
    pass


class MeetingSessions:
    def __init__(
        self,
        store: Store,
        approvals: ApprovalService,
        reply: Reply | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store, self.approvals, self.reply, self.clock = store, approvals, reply, clock
        self._locks: dict[str, threading.RLock] = {}
        self.initialize()

    def initialize(self) -> None:
        with self.store._connection(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meeting_sessions (
                    profile_id TEXT NOT NULL, meeting_id TEXT NOT NULL, initiator_id INTEGER NOT NULL,
                    participant_refs TEXT NOT NULL, allowed_assignee_refs TEXT NOT NULL,
                    previous_context TEXT, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open', next_sequence INTEGER NOT NULL DEFAULT 1,
                    finish_event_id TEXT, PRIMARY KEY(profile_id, meeting_id)
                );
                CREATE TABLE IF NOT EXISTS meeting_session_turns (
                    profile_id TEXT NOT NULL, meeting_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    request_id TEXT NOT NULL, sequence INTEGER NOT NULL, role TEXT NOT NULL,
                    text TEXT NOT NULL, PRIMARY KEY(profile_id, meeting_id, ordinal),
                    UNIQUE(profile_id, meeting_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS meeting_session_requests (
                    profile_id TEXT NOT NULL, meeting_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    route TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT,
                    PRIMARY KEY(profile_id, meeting_id, request_id)
                );
            """)

    def provision(
        self, profile_id: str, meeting_id: str, initiator_id: int, *,
        participant_refs: Sequence[str] = (), allowed_assignee_refs: Sequence[str] = (),
        previous_context: str | None = None, expires_at: datetime | None = None,
        ttl_seconds: int = 1800,
    ) -> str:
        now = self.clock()
        expiry = expires_at or (now + timedelta(seconds=ttl_seconds))
        if (expiry.tzinfo is None or expiry <= now or expiry > now + timedelta(hours=1)
                or initiator_id <= 0):
            raise ValueError("Session expiry must be in the future and initiator must be positive")
        token = secrets.token_urlsafe(32)
        with self.store._connection(write=True) as db:
            if db.execute("SELECT 1 FROM profiles WHERE profile_id=?", (profile_id,)).fetchone() is None:
                raise ValueError("Unknown profile")
            try:
                db.execute("""INSERT INTO meeting_sessions
                    (profile_id, meeting_id, initiator_id, participant_refs, allowed_assignee_refs,
                     previous_context, token_hash, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, meeting_id, initiator_id, json.dumps(list(participant_refs)),
                     json.dumps(list(allowed_assignee_refs)), previous_context,
                     self._hash(token), expiry.astimezone(UTC).isoformat()))
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise MeetingConflict("Meeting session already exists") from exc
                raise
        return token

    def start(self, meeting_id: str, token: str, request_id: str) -> dict[str, Any]:
        request_id = StartRequest(request_id=request_id).request_id
        session = self._authorized(meeting_id, token)
        profile_id = session["profile_id"]
        fingerprint = self._fingerprint("start", {"request_id": request_id})
        with self._lock(profile_id, meeting_id):
            session = self._authorized(meeting_id, token)
            previous = self._existing_request(profile_id, meeting_id, request_id, "start", fingerprint)
            if previous is not None:
                return json.loads(previous)
            if session["state"] != "open":
                raise MeetingConflict("Meeting is closed")
            with self.store._connection() as db:
                prior_start = db.execute(
                    "SELECT request_id FROM meeting_session_requests WHERE profile_id=? AND meeting_id=? AND route='start'",
                    (profile_id, meeting_id),
                ).fetchone()
            if prior_start is not None:
                raise MeetingConflict("Meeting was already started with another request_id")
            context = MeetingContext(profile_id, meeting_id, 0, session["previous_context"], (), None)
            try:
                reply = self._reply(context, "Я ИИ-помощник. С чего начнём?")
            except Exception:
                reply = "Я ИИ-помощник. С чего начнём?"
            result = {"reply_text": reply}
            with self.store._connection(write=True) as db:
                db.execute("INSERT INTO meeting_session_turns VALUES (?, ?, 0, ?, 0, 'assistant', ?)",
                           (profile_id, meeting_id, request_id, reply))
                self._save_request(db, profile_id, meeting_id, request_id, "start", fingerprint, result)
            return result

    def turn(self, meeting_id: str, token: str, body: TurnRequest) -> dict[str, Any]:
        if body.role != "participant":
            raise MeetingConflict("Only participant turns are accepted")
        session = self._authorized(meeting_id, token)
        profile_id = session["profile_id"]
        payload = body.model_dump()
        fingerprint = self._fingerprint("turns", payload)
        with self._lock(profile_id, meeting_id):
            session = self._authorized(meeting_id, token)
            previous = self._existing_request(profile_id, meeting_id, body.request_id, "turns", fingerprint)
            if previous is not None:
                return json.loads(previous)
            # Recover an input whose optional reply call was interrupted before its result was saved.
            with self.store._connection() as db:
                pending = db.execute(
                    "SELECT response FROM meeting_session_requests WHERE profile_id=? AND meeting_id=? AND request_id=?",
                    (profile_id, meeting_id, body.request_id),
                ).fetchone()
            if pending is not None:
                recovered = {"sequence": body.sequence, "reply_text": "Понял. Продолжайте, пожалуйста.",
                             "approval_required": False}
                with self.store._connection(write=True) as db:
                    db.execute("INSERT OR IGNORE INTO meeting_session_turns VALUES (?, ?, ?, ?, ?, 'assistant', ?)",
                               (profile_id, meeting_id, body.sequence * 2,
                                "assistant:" + hashlib.sha256(body.request_id.encode()).hexdigest(),
                                body.sequence, recovered["reply_text"]))
                    db.execute("UPDATE meeting_session_requests SET response=? WHERE profile_id=? AND meeting_id=? AND request_id=?",
                               (json.dumps(recovered), profile_id, meeting_id, body.request_id))
                return recovered
            if session["state"] != "open" or body.sequence != session["next_sequence"]:
                raise MeetingConflict("Meeting is closed or sequence is not next")
            with self.store._connection() as db:
                started = db.execute(
                    "SELECT 1 FROM meeting_session_requests WHERE profile_id=? AND meeting_id=? AND route='start'",
                    (profile_id, meeting_id),
                ).fetchone()
                interrupted = db.execute(
                    """SELECT 1 FROM meeting_session_requests WHERE profile_id=? AND meeting_id=?
                       AND route='turns' AND response IS NULL""",
                    (profile_id, meeting_id),
                ).fetchone()
            if started is None or interrupted is not None:
                raise MeetingConflict("Start the meeting or replay the interrupted turn first")
            context = self._context(session, body.sequence, body.text)
            # Persist user input before invoking an optional model; offline default never calls out.
            with self.store._connection(write=True) as db:
                db.execute("INSERT INTO meeting_session_turns VALUES (?, ?, ?, ?, ?, 'participant', ?)",
                           (profile_id, meeting_id, body.sequence * 2 - 1, body.request_id,
                            body.sequence, body.text.strip()))
                self._save_request(db, profile_id, meeting_id, body.request_id, "turns", fingerprint, None)
                db.execute("UPDATE meeting_sessions SET next_sequence = next_sequence + 1 WHERE profile_id=? AND meeting_id=?",
                           (profile_id, meeting_id))
            try:
                answer = self._reply(context, "Понял. Что ещё важно зафиксировать?")
            except Exception:
                # Keep the durable turn usable when an optional model adapter is unavailable.
                answer = "Понял. Продолжайте, пожалуйста."
            result = {"sequence": body.sequence, "reply_text": answer, "approval_required": False}
            with self.store._connection(write=True) as db:
                db.execute("INSERT INTO meeting_session_turns VALUES (?, ?, ?, ?, ?, 'assistant', ?)",
                           (profile_id, meeting_id, body.sequence * 2,
                            "assistant:" + hashlib.sha256(body.request_id.encode()).hexdigest(),
                            body.sequence, answer))
                db.execute("UPDATE meeting_session_requests SET response=? WHERE profile_id=? AND meeting_id=? AND request_id=?",
                           (json.dumps(result), profile_id, meeting_id, body.request_id))
            return result

    def finish(self, meeting_id: str, token: str, request_id: str) -> dict[str, Any]:
        request_id = FinishRequest(request_id=request_id).request_id
        session = self._authorized(meeting_id, token)
        profile_id = session["profile_id"]
        fingerprint = self._fingerprint("finish", {"request_id": request_id})
        with self._lock(profile_id, meeting_id):
            session = self._authorized(meeting_id, token)
            prior = self._existing_request(profile_id, meeting_id, request_id, "finish", fingerprint)
            if prior is not None:
                return json.loads(prior)
            with self.store._connection() as db:
                pending_turn = db.execute(
                    "SELECT 1 FROM meeting_session_requests WHERE profile_id=? AND meeting_id=? AND route='turns' AND response IS NULL",
                    (profile_id, meeting_id),
                ).fetchone()
            if pending_turn is not None:
                raise MeetingConflict("request_in_progress")
            turns = self._turns(profile_id, meeting_id)
            if not any(item.role == "participant" for item in turns):
                raise MeetingConflict("Cannot finish a meeting without participant turns")
            source = MeetingSource(
                meeting_id=meeting_id, initiator=TelegramActor(telegram_user_id=session["initiator_id"]),
                participant_refs=tuple(json.loads(session["participant_refs"])),
                allowed_assignee_refs=tuple(json.loads(session["allowed_assignee_refs"])), turns=turns)
            with self.store._connection(write=True) as db:
                db.execute("UPDATE meeting_sessions SET state='finishing' WHERE profile_id=? AND meeting_id=? AND state='open'",
                           (profile_id, meeting_id))
            try:
                event_id = session["finish_event_id"] or self.approvals.submit_transcript(profile_id, source)
            except ValueError as exc:
                raise MeetingConflict(str(exc)) from exc
            result = {"status": "queued", "meeting_id": meeting_id}
            with self.store._connection(write=True) as db:
                db.execute("UPDATE meeting_sessions SET state='finished', finish_event_id=? WHERE profile_id=? AND meeting_id=?",
                           (event_id, profile_id, meeting_id))
                self._save_request(db, profile_id, meeting_id, request_id, "finish", fingerprint, result)
            return result

    def get_finished_event(self, profile_id: str, meeting_id: str) -> str | None:
        """Return the inbox event created by finish, for the offline processor/CLI."""
        with self.store._connection() as db:
            row = db.execute(
                "SELECT finish_event_id FROM meeting_sessions WHERE profile_id=? AND meeting_id=?",
                (profile_id, meeting_id),
            ).fetchone()
        return row["finish_event_id"] if row is not None else None

    def _authorized(self, meeting_id: str, token: str):
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM meeting_sessions WHERE meeting_id=? AND token_hash=?",
                             (meeting_id, self._hash(token))).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= self.clock():
            raise MeetingUnauthorized("Invalid or expired meeting token")
        return row

    def _context(self, session, sequence: int, participant_text: str) -> MeetingContext:
        transcript = self._turns(session["profile_id"], session["meeting_id"])
        values = tuple({"role": turn.role, "text": turn.text} for turn in transcript)
        return MeetingContext(session["profile_id"], session["meeting_id"], sequence,
                              session["previous_context"], values, participant_text)

    def _turns(self, profile_id: str, meeting_id: str) -> tuple[TranscriptTurn, ...]:
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM meeting_session_turns WHERE profile_id=? AND meeting_id=? ORDER BY ordinal",
                              (profile_id, meeting_id)).fetchall()
        result = []
        for row in rows:
            if row["sequence"] > 0:
                result.append(TranscriptTurn(request_id=row["request_id"], sequence=len(result) + 1,
                                             text=row["text"], role=row["role"]))
        return tuple(result)

    def _reply(self, context: MeetingContext, fallback: str) -> str:
        text = self.reply(context) if self.reply is not None else fallback
        return str(text).strip() or fallback

    def _existing_request(self, profile_id, meeting_id, request_id, route, fingerprint):
        with self.store._connection() as db:
            row = db.execute("SELECT route,fingerprint,response FROM meeting_session_requests WHERE profile_id=? AND meeting_id=? AND request_id=?",
                             (profile_id, meeting_id, request_id)).fetchone()
        if row is None:
            return None
        if row["route"] != route or row["fingerprint"] != fingerprint:
            raise MeetingConflict("request_id was reused with a different request")
        if row["response"] is None and route != "turns":
            raise MeetingConflict("request_in_progress")
        return row["response"]

    @staticmethod
    def _save_request(db, profile_id, meeting_id, request_id, route, fingerprint, response):
        db.execute("INSERT INTO meeting_session_requests VALUES (?, ?, ?, ?, ?, ?)",
                   (profile_id, meeting_id, request_id, route, fingerprint,
                    json.dumps(response) if response is not None else None))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _fingerprint(route: str, payload: dict) -> str:
        return hashlib.sha256(json.dumps([route, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _lock(self, profile_id: str, meeting_id: str) -> threading.RLock:
        return self._locks.setdefault(f"{profile_id}:{meeting_id}", threading.RLock())


def router(sessions: MeetingSessions | None = None) -> APIRouter:
    api = APIRouter(prefix="/v1/meetings")

    def service(request: Request) -> MeetingSessions:
        value = sessions or getattr(request.app.state, "meeting_sessions", None)
        if value is None:
            raise HTTPException(503, detail={"code": "meeting_unavailable", "message": "Meeting service unavailable"})
        return value

    def bearer(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, detail={"code": "unauthorized", "message": "Session token required"})
        return header[7:].strip()

    @api.post("/{meeting_id}/start")
    def start(meeting_id: str, body: StartRequest, request: Request):
        try:
            return service(request).start(meeting_id, bearer(request), body.request_id)
        except MeetingUnauthorized as exc:
            raise HTTPException(401, detail={"code": "unauthorized", "message": str(exc)}) from exc
        except MeetingConflict as exc:
            raise HTTPException(409, detail={"code": "conflict", "message": str(exc)}) from exc

    @api.post("/{meeting_id}/turns")
    def turns(meeting_id: str, body: TurnRequest, request: Request):
        try:
            return service(request).turn(meeting_id, bearer(request), body)
        except MeetingUnauthorized as exc:
            raise HTTPException(401, detail={"code": "unauthorized", "message": str(exc)}) from exc
        except MeetingConflict as exc:
            raise HTTPException(409, detail={"code": "conflict", "message": str(exc)}) from exc

    @api.post("/{meeting_id}/finish", status_code=202)
    def finish(meeting_id: str, body: FinishRequest, request: Request):
        try:
            return service(request).finish(meeting_id, bearer(request), body.request_id)
        except MeetingUnauthorized as exc:
            raise HTTPException(401, detail={"code": "unauthorized", "message": str(exc)}) from exc
        except MeetingConflict as exc:
            raise HTTPException(409, detail={"code": "conflict", "message": str(exc)}) from exc

    return api
