from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.core.approvals import ApprovalService
from app.core.demo import seed_demo
from app.core.meeting_sessions import MeetingConflict, MeetingSessions, MeetingUnauthorized, TurnRequest
from app.core.store import Store


@pytest.fixture
def setup(tmp_path: Path):
    store = Store(tmp_path / "test.sqlite")
    store.initialize()
    seed_demo(store)
    sessions = MeetingSessions(store, ApprovalService(store), reply=lambda ctx: f"Ответ на {ctx.sequence}")
    token = sessions.provision("demo-p1", "m-demo", 12345, participant_refs=("person-a",))
    return store, sessions, token


def test_full_flow_and_replay_survive_recreation(setup, tmp_path):
    store, sessions, token = setup
    assert sessions.start("m-demo", token, "s1")["reply_text"]
    body = {"request_id": "r1", "sequence": 1, "text": "Релиз в пятницу", "role": "participant"}
    first = sessions.turn("m-demo", token, TurnRequest(**body))
    restarted = MeetingSessions(store, ApprovalService(store), reply=lambda ctx: "не должен вызываться")
    assert restarted.turn("m-demo", token, TurnRequest(**body)) == first
    assert restarted.finish("m-demo", token, "f1") == {"status": "queued", "meeting_id": "m-demo"}
    assert restarted.finish("m-demo", token, "f2") == {"status": "queued", "meeting_id": "m-demo"}
    assert restarted.get_finished_event("demo-p1", "m-demo")
    with store._connection() as db:
        rows = db.execute("SELECT * FROM inbox_events WHERE source='meeting'").fetchall()
        assert len(rows) == 1


def test_conflicting_replay_sequence_and_expiry(setup):
    _, sessions, token = setup
    sessions.start("m-demo", token, "s1")
    sessions.turn("m-demo", token, TurnRequest(request_id="r1", sequence=1, text="ok"))
    with pytest.raises(MeetingConflict):
        sessions.turn("m-demo", token, TurnRequest(request_id="r1", sequence=1, text="changed"))
    with pytest.raises(MeetingConflict):
        sessions.turn("m-demo", token, TurnRequest(request_id="r2", sequence=3, text="gap"))
    with pytest.raises(MeetingUnauthorized):
        sessions.start("other", token, "s1")


def test_expired_token_is_rejected(tmp_path):
    store = Store(tmp_path / "expired.sqlite")
    store.initialize()
    seed_demo(store)
    now = datetime(2026, 11, 28, tzinfo=UTC)
    sessions = MeetingSessions(store, ApprovalService(store), clock=lambda: now)
    token = sessions.provision("demo-p1", "old", 12345, expires_at=now + timedelta(seconds=1))
    later = MeetingSessions(store, ApprovalService(store), clock=lambda: now + timedelta(seconds=2))
    with pytest.raises(MeetingUnauthorized):
        later.start("old", token, "s1")


def test_two_requests_cannot_claim_the_same_sequence(setup):
    _, sessions, token = setup
    sessions.start("m-demo", token, "s1")
    def send(request_id):
        try:
            return sessions.turn("m-demo", token, TurnRequest(
                request_id=request_id, sequence=1, text="same slot"))
        except MeetingConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, ("r1", "r2")))
    assert sum(result != "conflict" for result in results) == 1

