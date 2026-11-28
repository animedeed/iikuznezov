from pathlib import Path

from app.adapters.model import ModelAdapterError
from app.core.approvals import ApprovalService, MeetingSource, TranscriptTurn
from app.core.demo import seed_demo
from app.core.meeting_processing import process_pending_meetings
from app.core.store import Store
from app.offline_demo import run
from app.testing import FakeModel
from app.workflow.minutes import MinutesDraft


def test_offline_acceptance_report():
    report = run(Path("docs/CONTRACT_EXAMPLES.json"))
    assert report == {
        "mode": "offline",
        "mail": {"m001": "approved_after_review", "m002": "automatic", "simulated_email_deliveries": 2},
        "meeting": {
            "draft_version": 1, "tasks_created": 1, "calendar_events": 1,
            "calendar_retried": True, "plan_tasks": 1, "isolated_profiles": True,
        },
        "external_calls": 0,
    }


def test_model_failure_preserves_profile_scoped_transcript(tmp_path):
    store = Store(tmp_path / "pipeline.sqlite3")
    store.initialize()
    seed_demo(store)
    source = MeetingSource(
        meeting_id="m", initiator={"telegram_user_id": 10001},
        participant_refs=(), allowed_assignee_refs=(),
        turns=(TranscriptTurn(request_id="t1", sequence=1, text="Отложим до следующей встречи."),),
    )
    approvals = ApprovalService(store)
    event = approvals.submit_transcript("demo-p1", source)
    other_event = approvals.submit_transcript("demo-p2", source)
    failed = process_pending_meetings(
        store, FakeModel(answer=ModelAdapterError("timeout")), profile_id="demo-p1",
    )
    assert failed == {"processed": [], "errors": [{"event_id": event, "error_code": "timeout"}]}
    retry = process_pending_meetings(
        Store(store.path), FakeModel(answer=MinutesDraft(summary="Итог")), profile_id="demo-p1",
    )
    assert len(retry["processed"]) == 1 and retry["processed"][0]["event_id"] == event
    with store._connection() as db:
        assert db.execute(
            "SELECT status FROM inbox_events WHERE profile_id = ? AND event_id = ?",
            ("demo-p2", other_event),
        ).fetchone()[0] == "received"



def test_avatar_http_routes_use_the_shared_database(tmp_path):
    from asyncio import run as run_async

    from httpx import ASGITransport, AsyncClient

    from app.config import Settings
    from app.main import create_app

    application = create_app(Settings(database_path=tmp_path / "http-meeting.sqlite3"))

    async def scenario():
        async with application.router.lifespan_context(application):
            store = application.state.store
            seed_demo(store)
            sessions = application.state.meeting_sessions
            token = sessions.provision("demo-p1", "http-meeting", 10001)
            headers = {"Authorization": f"Bearer {token}"}
            async with AsyncClient(transport=ASGITransport(app=application), base_url="http://testserver") as client:
                base = "/v1/meetings/http-meeting"
                denied = await client.post(f"{base}/start", json={"request_id": "start"})
                assert denied.status_code == 401
                reserved = await client.post(
                    f"{base}/start", headers=headers, json={"request_id": "assistant:reserved"},
                )
                assert reserved.status_code == 422
                first = await client.post(f"{base}/start", headers=headers, json={"request_id": "start"})
                assert first.status_code == 200 and first.json()["reply_text"]
                turn = {"request_id": "turn-1", "sequence": 1, "text": "Готовим черновик."}
                for invalid in ({"text": "   "}, {"request_id": "   "}, {"request_id": "assistant:reserved"}):
                    bad = await client.post(f"{base}/turns", headers=headers, json={**turn, **invalid})
                    assert bad.status_code == 422
                response = await client.post(f"{base}/turns", headers=headers, json=turn)
                assert response.status_code == 200
                conflict = await client.post(f"{base}/turns", headers=headers, json={**turn, "text": "Иной текст"})
                assert conflict.status_code == 409
                for request_id in ("finish", "repeat-finish"):
                    finish = await client.post(f"{base}/finish", headers=headers, json={"request_id": request_id})
                    assert finish.status_code == 202
                replay = await client.post(f"{base}/start", headers=headers, json={"request_id": "start"})
                assert replay.json() == first.json()
            event_id = sessions.get_finished_event("demo-p1", "http-meeting")
            assert event_id is not None
            result = process_pending_meetings(
                store, FakeModel(answer=MinutesDraft(summary="Учебный итог")), profile_id="demo-p1",
            )
            assert [row["event_id"] for row in result["processed"]] == [event_id]

    run_async(scenario())

