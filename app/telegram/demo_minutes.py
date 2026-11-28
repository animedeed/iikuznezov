"""Synthetic meeting and editable fields for the standalone Telegram demo."""

from datetime import UTC, date, datetime

from app.contracts import EffectKind, MinutesCandidate, TaskDraft, TelegramActor
from app.core.approvals import ApprovalService, EffectIntent, MeetingSource


RECIPIENTS = ("demo-team", "demo-manager")
ASSIGNEES = ("demo-alex", "demo-maria")
REVISION_HINT = (
    "В личном чате: /revise summary <текст>, /revise task <текст>, "
    "/revise assignee <demo-alex|demo-maria>, /revise due <ГГГГ-ММ-ДД> "
    "или /revise recipient <demo-team|demo-manager>."
)


def prepare_minutes(approvals: ApprovalService, user_id: int) -> tuple[str, MinutesCandidate]:
    meeting_id = "telegram-demo-meeting"
    source = approvals.submit_transcript("telegram-demo", MeetingSource(
        meeting_id=meeting_id,
        initiator=TelegramActor(telegram_user_id=user_id),
        participant_refs=RECIPIENTS,
        allowed_assignee_refs=ASSIGNEES,
        turns=(),
    ))
    candidate = MinutesCandidate(
        meeting_id=meeting_id, title="Планёрка по поставке",
        occurred_at=datetime(2026, 12, 7, 7, tzinfo=UTC),
        participant_refs=(RECIPIENTS[0],),
        summary="Команда уточнит сроки поставки и сообщит их клиенту.",
        decisions=(), open_questions=(),
        tasks=(TaskDraft(
            task_ref="demo-delivery", action="Уточнить срок поставки",
            assignee_ref=None, due_date=None, status="needs_clarification",
        ),),
    )
    return source, candidate


def minutes_intents(candidate: MinutesCandidate) -> list[EffectIntent]:
    return [
        *(EffectIntent(kind=EffectKind.EMAIL, target_ref=ref) for ref in candidate.participant_refs),
        *(EffectIntent(kind=EffectKind.TASK, target_ref=task.task_ref)
          for task in candidate.tasks if task.status == "draft"),
    ]


def revise_minutes(candidate: MinutesCandidate, entry: str) -> MinutesCandidate:
    field, _, value = entry.partition(" ")
    value = value.strip()
    if not value:
        raise ValueError(REVISION_HINT)
    data = candidate.model_dump()
    task = data["tasks"][0]
    if field == "summary":
        data["summary"] = value
    elif field == "task":
        task["action"] = value
    elif field == "assignee" and value in ASSIGNEES:
        task["assignee_ref"] = value
    elif field == "due":
        try:
            due = date.fromisoformat(value)
        except ValueError:
            raise ValueError("Укажите срок в формате ГГГГ-ММ-ДД.") from None
        if due.isoformat() != value:
            raise ValueError("Укажите срок в формате ГГГГ-ММ-ДД.")
        task["due_date"] = due
    elif field == "recipient" and value in RECIPIENTS:
        data["participant_refs"] = (value,)
    else:
        raise ValueError(REVISION_HINT)
    task["status"] = "draft" if task["assignee_ref"] and task["due_date"] else "needs_clarification"
    return MinutesCandidate.model_validate(data)
