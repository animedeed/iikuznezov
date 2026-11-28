"""One repeatable acceptance run using only local fixtures and simulated providers."""

from argparse import ArgumentParser
from datetime import timedelta
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory

from app.calendar.fake import FakeCalendar
from app.calendar.ports import CalendarTemporaryError
from app.contracts import (
    Decision, DecisionStatus, DeliveryStatus, Effect, EffectKind, EffectResult,
    MailCandidate, MailReceipt, PolicyClass, TelegramActor,
)
from app.core.approvals import ApprovalService
from app.core.demo import seed_demo
from app.core.effects import EffectQueue
from app.core.mail_intake import process_mail
from app.core.meeting_processing import process_pending_meetings, process_saved_meeting
from app.core.meeting_sessions import MeetingSessions, TurnRequest
from app.core.store import Store
from app.testing import FakeModel
from app.workflow.followups import load_meeting_source, sweep_approved_minutes
from app.workflow.minutes import MinutesDraft
from app.workflow.plan import build_plan, schedule_plan_message
from app.workflow.tasks import SqliteTaskStore
from app.workflow.transport import WorkflowTransport

_PROFILE = "demo-p1"
_LEADER = TelegramActor(telegram_user_id=10001)
_INITIATOR = TelegramActor(telegram_user_id=20001)


class OfflineTransport:
    """Routes local task/calendar effects and records simulated email and notices."""

    def __init__(self, workflow: WorkflowTransport):
        self.workflow = workflow
        self.calls: list[Effect] = []

    def execute(self, effect: Effect) -> EffectResult:
        self.calls.append(effect)
        if effect.kind in {EffectKind.TASK, EffectKind.CALENDAR_EVENT}:
            return self.workflow.execute(effect)
        return EffectResult(status=DeliveryStatus.SENT, external_id=f"offline-{effect.effect_id}")


def _verify(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError(f"Offline check did not pass: {description}")


def _drain(store: Store, transport: OfflineTransport) -> None:
    queue = EffectQueue(store)
    while queue.run_once(transport) is not None:
        pass


def _receipt(fixtures: dict, fixture_id: str) -> MailReceipt:
    fixture = next(item for item in fixtures["mail_receipts"] if item["fixture_id"] == fixture_id)
    return MailReceipt.model_validate({
        key: value for key, value in fixture.items() if key not in {"channel_id", "fixture_id"}
    })


def _check_mail(store: Store, transport: OfflineTransport, fixtures: dict) -> dict:
    sensitive = MailCandidate(
        topic="customer_commitment", requires_review=True, policy_class=PolicyClass.CUSTOMER_COMMITMENT,
        rationale="Подтверждение срока поставки требует решения руководителя.",
    )
    quote = MailCandidate(topic="quote_request", requires_review=False, rationale="Тестовый запрос КП.")
    m001 = process_mail(
        store, "demo-mail-sales", _receipt(fixtures, "m001"), sensitive, sales_alias="sales-desk",
    )
    m002 = process_mail(
        store, "demo-mail-sales", _receipt(fixtures, "m002"), quote, sales_alias="sales-desk", trusted_quote=True,
    )
    replay = process_mail(
        store, "demo-mail-sales", _receipt(fixtures, "m002"), quote, sales_alias="sales-desk", trusted_quote=True,
    )
    _verify(m001.approval_id is not None, "m001 approval is present")
    _verify(m002.effect_id is not None and m002.effect_id == replay.effect_id, "m002 replay uses one effect")
    _drain(store, transport)
    emails = [effect for effect in transport.calls if effect.kind == EffectKind.EMAIL]
    _verify([effect.effect_id for effect in emails] == [m002.effect_id], "only m002 can send before approval")
    approvals = ApprovalService(Store(store.path))
    _verify(approvals.decide(m001.approval_id, _LEADER, Decision.APPROVE) == DecisionStatus.ACCEPTED,
            "m001 can be approved by its leader")
    _verify(approvals.decide(m001.approval_id, _LEADER, Decision.APPROVE) == DecisionStatus.ALREADY_DECIDED,
            "m001 repeat approval stays closed")
    _drain(store, transport)
    emails = [effect for effect in transport.calls if effect.kind == EffectKind.EMAIL]
    _verify(len(emails) == 2, "one simulated email for each mail fixture")
    return {"m001": "approved_after_review", "m002": "automatic", "simulated_email_deliveries": len(emails)}


def _check_meeting(
    store: Store, transport: OfflineTransport, calendar: FakeCalendar, tasks: SqliteTaskStore, fixtures: dict,
) -> dict:
    approvals = ApprovalService(store)
    template = fixtures["minutes_version"]
    meeting_id = template["meeting_id"]
    session = MeetingSessions(store, approvals)
    token = session.provision(
        _PROFILE, meeting_id, _INITIATOR.telegram_user_id,
        participant_refs=tuple(template["participant_refs"]), allowed_assignee_refs=("sales-desk",),
    )
    session.start(meeting_id, token, "offline-start")
    for turn in fixtures["meeting_turns"]:
        session.turn(meeting_id, token, TurnRequest.model_validate(turn))
    finish = session.finish(meeting_id, token, "offline-finish")
    _verify(finish == session.finish(meeting_id, token, "offline-finish-repeat"),
            "meeting finishes only once even with another request ID")
    event_id = session.get_finished_event(_PROFILE, meeting_id)
    _verify(event_id is not None, "meeting finish saved the transcript")
    source, occurred_at = load_meeting_source(store, _PROFILE, event_id)
    _verify(approvals.submit_transcript(_PROFILE, source) == event_id, "transcript replay uses one input")
    changed = source.model_copy(update={"turns": ()})
    try:
        approvals.submit_transcript(_PROFILE, changed)
    except ValueError:
        pass
    else:
        raise RuntimeError("A changed final transcript reused the same meeting ID")

    due = occurred_at.date() + timedelta(days=3)
    fake_model = FakeModel(answer=MinutesDraft.model_validate({
        "title": template["title"], "summary": template["summary"],
        "decisions": template["decisions"], "open_questions": template["open_questions"],
        "tasks": [
            {**template["tasks"][0], "due_date": due.isoformat()},
            template["tasks"][1],
        ],
    }))
    batch = process_pending_meetings(store, fake_model, profile_id=_PROFILE)
    _verify(len(batch["processed"]) == 1 and not batch["errors"], "one saved minutes draft")
    version = process_saved_meeting(store, fake_model, _PROFILE, event_id)
    _verify(len(fake_model.calls) == 1, "minutes replay does not call the model")
    candidate = version.candidate
    _verify(candidate.kind == "minutes" and candidate.tasks[1].status == "needs_clarification",
            "the incomplete task remains for clarification")
    with store._connection() as db:
        row = db.execute(
            """SELECT approval_id FROM approvals
               WHERE profile_id = ? AND document_id = ? AND version = ?""",
            (_PROFILE, version.document_id, version.version),
        ).fetchone()
    approval_id = row["approval_id"]
    _verify(approvals.decide(approval_id, _INITIATOR, Decision.APPROVE) == DecisionStatus.ACCEPTED,
            "meeting initiator approves the draft")
    _verify(process_saved_meeting(Store(store.path), fake_model, _PROFILE, event_id) == version,
            "approved minutes recover after restart")

    sweep_approved_minutes(store)
    sweep_approved_minutes(store)
    calendar.fail_next(CalendarTemporaryError())
    _drain(store, transport)
    with store._connection() as db:
        effects = db.execute(
            """SELECT effect_id, kind, status FROM effects
               WHERE profile_id = ? AND document_id = ? AND kind IN ('email','task','calendar_event')""",
            (_PROFILE, version.document_id),
        ).fetchall()
    statuses = sorted((row["kind"], row["status"]) for row in effects)
    _verify(statuses == [("calendar_event", "retryable"), ("email", "sent"), ("email", "sent"), ("task", "sent")],
            "calendar failure does not block other operations")
    calendar_id = next(row["effect_id"] for row in effects if row["kind"] == "calendar_event")
    reopened = Store(store.path)
    EffectQueue(reopened).retry(calendar_id)
    _drain(reopened, transport)
    _verify(EffectQueue(reopened).status(calendar_id).status == "sent", "calendar retry finishes")
    _verify(len(calendar.written) == 1 and len(tasks.list_open(_PROFILE)) == 1,
            "there is one calendar event and one complete task")
    _verify(not tasks.list_open("demo-p2"), "the task is isolated by profile")
    plan = build_plan(_PROFILE, due, "day", store=store, calendar=calendar, tasks=tasks)
    _verify(len(plan.tasks_due) == 1, "the task appears in the daily plan")
    _verify(schedule_plan_message(store, plan) and not schedule_plan_message(store, plan),
            "one queued plan per local day")
    return {
        "draft_version": version.version, "tasks_created": len(tasks.list_open(_PROFILE)),
        "calendar_events": len(calendar.written), "calendar_retried": True,
        "plan_tasks": len(plan.tasks_due), "isolated_profiles": True,
    }


def run(fixtures: Path) -> dict:
    data = loads(fixtures.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="iikuznezov-offline-") as directory:
        store = Store(Path(directory) / "smoke.sqlite3")
        store.initialize()
        seed_demo(store)
        calendar = FakeCalendar()
        tasks = SqliteTaskStore(store)
        transport = OfflineTransport(WorkflowTransport(calendar, tasks))
        mail = _check_mail(store, transport, data)
        meeting = _check_meeting(store, transport, calendar, tasks, data)
    return {"mode": "offline", "mail": mail, "meeting": meeting, "external_calls": 0}


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("docs/CONTRACT_EXAMPLES.json"))
    args = parser.parse_args()
    print(dumps(run(args.fixtures), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
