"""Synthetic plan provider used by the standalone demo."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.contracts import Plan, PlanMeeting, PlanTask
from app.telegram.plans import Period


def build_demo_plan(profile_id: str, when: date, period: Period) -> Plan:
    timezone = "Europe/Moscow"
    meeting_at = datetime.combine(when, time(10), ZoneInfo(timezone)).astimezone(UTC)
    previous = when - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    tasks = [PlanTask(id="demo-delivery", title="Уточнить срок поставки", due_date=when)]
    if period == "week":
        tasks.append(PlanTask(
            id="demo-follow-up", title="Сообщить клиенту результат",
            due_date=when + timedelta(days=4),
        ))
    return Plan(
        profile_id=profile_id, date=when, period=period, timezone=timezone,
        meetings=(PlanMeeting(id="demo-meeting", title="Планёрка по поставке", starts_at=meeting_at),),
        tasks_due=tuple(tasks),
        overdue=(PlanTask(id="demo-old", title="Обновить список заявок", due_date=previous),),
        previous_workday=previous, previous_workday_results=("Согласовали черновик письма.",),
        mail_statuses=("Запрос клиента принят.",),
        pending_approvals=("Черновик ответа по срокам поставки",),
    )
