"""Day and week plans built from the calendar, approved tasks and the local queue/approval state."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from app.calendar.ports import CalendarPort
from app.contracts import Effect, EffectKind, Plan, PlanMeeting, PlanTask
from app.core.effects import EffectConflict, enqueue_effect
from app.core.store import Store
from app.workflow.tasks import TaskSource

MON_FRI = frozenset({0, 1, 2, 3, 4})
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_KIND_LABELS = {
    "email": "Отправлено писем", "task": "Создано поручений",
    "calendar_event": "Создано событий в календаре", "telegram": "Отправлено сообщений в Telegram",
}


def previous_workday(day: date, workdays: Collection[int] = MON_FRI) -> date:
    if not workdays or not set(workdays).issubset(range(7)):
        raise ValueError("workdays must be a non-empty collection of weekdays (0–6)")
    probe = day - timedelta(days=1)
    while probe.weekday() not in workdays:
        probe -= timedelta(days=1)
    return probe


def _window(day: date, days: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=days), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def build_plan(
    profile_id: str, day: date, period: str = "day", *, store: Store, calendar: CalendarPort,
    tasks: TaskSource, workdays: Collection[int] = MON_FRI,
) -> Plan:
    """`day` is a local date of the profile. A week plan runs from `day` to the end of its ISO week.
    Calendar and task failures propagate: an empty plan must never mean "could not read"."""
    if period not in {"day", "week"}:
        raise ValueError("period must be day or week")
    profile = store.get_profile(profile_id)
    if profile is None:
        raise LookupError("Unknown profile")
    tz = ZoneInfo(profile.timezone)
    span = 1 if period == "day" else 7 - day.weekday()
    start, end = _window(day, span, tz)
    last_day = day + timedelta(days=span - 1)

    meetings = tuple(sorted(
        (PlanMeeting(id=e.id, title=e.title, starts_at=e.starts_at)
         for e in calendar.list_events(profile_id, start, end) if not e.all_day),
        key=lambda m: (m.starts_at, m.id),
    ))
    open_tasks = sorted(tasks.list_open(profile_id), key=lambda t: (t.due_date, t.id))
    due = tuple(PlanTask(id=t.id, title=t.title, due_date=t.due_date)
                for t in open_tasks if day <= t.due_date <= last_day)
    overdue = tuple(PlanTask(id=t.id, title=t.title, due_date=t.due_date)
                    for t in open_tasks if t.due_date < day)

    prev = previous_workday(day, workdays)
    prev_start, prev_end = _window(prev, 1, tz)
    with store._connection() as db:
        results = _previous_results(db, profile_id, prev_start, prev_end)
        mail = _mail_statuses(db, profile_id)
        pending = tuple(r["approval_id"] for r in db.execute(
            "SELECT approval_id FROM approvals WHERE profile_id = ? AND state = 'pending_approval' "
            "ORDER BY created_at, approval_id", (profile_id,)))
    return Plan(
        profile_id=profile_id, date=day, period=period, timezone=profile.timezone,
        meetings=meetings, tasks_due=due, overdue=overdue, previous_workday=prev,
        previous_workday_results=results, mail_statuses=mail, pending_approvals=pending,
    )


def _previous_results(db, profile_id: str, start: datetime, end: datetime) -> tuple[str, ...]:
    # updated_at is an ISO-8601 UTC string written by the core, so bounds compare lexicographically.
    lo, hi = start.isoformat(), end.isoformat()
    lines = []
    for row in db.execute(
        """SELECT kind, count(*) AS n FROM effects
        WHERE profile_id = ? AND status = 'sent' AND updated_at >= ? AND updated_at < ?
          AND NOT (kind = 'telegram' AND json_extract(payload, '$.operation') = 'approval_notice')
        GROUP BY kind ORDER BY kind""", (profile_id, lo, hi)):
        lines.append(f"{_KIND_LABELS[row['kind']]}: {row['n']}")
    decisions = {r["decision"]: r["n"] for r in db.execute(
        """SELECT decision, count(*) AS n FROM approvals
        WHERE profile_id = ? AND decided_at >= ? AND decided_at < ? GROUP BY decision""", (profile_id, lo, hi))}
    if decisions:
        lines.append("Решения по согласованиям: утверждено {}, отклонено {}, на правку {}".format(
            decisions.get("approve", 0), decisions.get("reject", 0), decisions.get("revise", 0)))
    return tuple(lines)


def _mail_statuses(db, profile_id: str) -> tuple[str, ...]:
    lines = []
    waiting = db.execute(
        "SELECT count(*) FROM inbox_events WHERE profile_id = ? AND source = 'mail' AND status = 'received'",
        (profile_id,)).fetchone()[0]
    if waiting:
        lines.append(f"Писем ожидают обработки: {waiting}")
    review = db.execute(
        """SELECT count(*) FROM approvals AS a JOIN documents AS d
        ON d.profile_id = a.profile_id AND d.document_id = a.document_id
        WHERE a.profile_id = ? AND a.state = 'pending_approval' AND d.kind = 'mail_action'""",
        (profile_id,)).fetchone()[0]
    if review:
        lines.append(f"Писем ждут решения руководителя: {review}")
    for row in db.execute(
        """SELECT status, count(*) AS n FROM effects WHERE profile_id = ? AND kind = 'email'
        AND status IN ('retryable', 'unknown', 'failed') GROUP BY status ORDER BY status""", (profile_id,)):
        label = {"retryable": "ждут повторной отправки", "unknown": "с неизвестным исходом, нужна сверка",
                 "failed": "не доставлены"}[row["status"]]
        lines.append(f"Писем {label}: {row['n']}")
    return tuple(lines)


def format_plan(plan: Plan) -> str:
    zone = ZoneInfo(plan.timezone)
    title = "План дня" if plan.period == "day" else "План недели"
    out = [f"{title}, {plan.date:%d.%m.%Y}"]
    meetings = [(m.starts_at.astimezone(zone), m) for m in plan.meetings]
    tasks = list(plan.tasks_due)

    def block(name: str, rows: list[str]) -> None:
        out.append(f"\n{name}:")
        out.extend(rows or ["— нет"])

    if plan.period == "day":
        block("Встречи", [f"• {at:%H:%M} {m.title}" for at, m in meetings])
        block("Сроки", [f"• {t.title}" for t in tasks])
    else:
        days = sorted({at.date() for at, _ in meetings} | {t.due_date for t in tasks})
        for day in days:
            rows = [f"• {at:%H:%M} {m.title}" for at, m in meetings if at.date() == day]
            rows += [f"• срок: {t.title}" for t in tasks if t.due_date == day]
            block(f"{_WEEKDAYS[day.weekday()]} {day:%d.%m}", rows)
        if not days:
            block("Встречи и сроки", [])
    if plan.overdue:
        block("Просрочено", [f"• {t.due_date:%d.%m} {t.title}" for t in plan.overdue])
    block(f"Итоги {plan.previous_workday:%d.%m}", [f"• {r}" for r in plan.previous_workday_results])
    if plan.mail_statuses:
        block("Почта", [f"• {s}" for s in plan.mail_statuses])
    if plan.pending_approvals:
        out.append(f"\nЖдут вашего решения: {len(plan.pending_approvals)}")
    return "\n".join(out)


def schedule_plan_message(store: Store, plan: Plan) -> bool:
    """Queue one Telegram message per profile, local date and period. False if already queued.
    Payload contract for the Telegram owner: {"operation": "plan_message", "telegram_user_id", "text"}."""
    with store._connection(write=True) as db:
        leaders = db.execute(
            "SELECT channel_id FROM bindings WHERE profile_id = ? AND kind = 'telegram' AND active = 1",
            (plan.profile_id,)).fetchall()
        if len(leaders) != 1:
            raise LookupError("Profile needs exactly one active Telegram binding")
        key = _plan_key(plan.profile_id, plan.date, plan.period)
        if db.execute("SELECT 1 FROM effects WHERE profile_id = ? AND key = ?",
                      (plan.profile_id, key)).fetchone() is not None:
            return False  # enqueue_effect would silently accept an exact retry, so check first
        try:
            enqueue_effect(db, Effect(
                effect_id=uuid5(NAMESPACE_URL, key).hex, profile_id=plan.profile_id, key=key,
                kind=EffectKind.TELEGRAM, target_ref=f"plan-{plan.date.isoformat()}-{plan.period}",
                payload={"operation": "plan_message", "telegram_user_id": int(leaders[0]["channel_id"]),
                         "text": format_plan(plan)},
            ))
        except EffectConflict:
            return False
        return True


def _plan_key(profile_id: str, day: date, period: str) -> str:
    return f"plan:{profile_id}:{day.isoformat()}:{period}"


def send_due_plans(
    now: datetime, *, store: Store, calendar: CalendarPort, tasks: TaskSource,
    at: time = time(8, 30), workdays: Collection[int] = MON_FRI,
    profile_ids: Iterable[str] | None = None,
) -> list[str]:
    """For each profile whose local time has passed `at` on a workday, queue today's plan.
    Only the current local day is considered, so a late start never sends a stale plan."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    selected = None if profile_ids is None else set(profile_ids)
    with store._connection() as db:
        profiles = db.execute(
            """SELECT profile_id, timezone FROM profiles JOIN bindings USING (profile_id)
            WHERE kind = 'telegram' AND active = 1
            GROUP BY profile_id, timezone HAVING count(*) = 1 ORDER BY profile_id""",
        ).fetchall()
    queued = []
    for profile in profiles:
        profile_id = profile["profile_id"]
        if selected is not None and profile_id not in selected:
            continue
        local = now.astimezone(ZoneInfo(profile["timezone"]))
        if local.weekday() not in workdays or local.time() < at:
            continue
        with store._connection() as db:
            if db.execute("SELECT 1 FROM effects WHERE profile_id = ? AND key = ?",
                          (profile_id, _plan_key(profile_id, local.date(), "day"))).fetchone():
                continue
        plan = build_plan(profile_id, local.date(), "day", store=store, calendar=calendar,
                          tasks=tasks, workdays=workdays)
        if schedule_plan_message(store, plan):
            queued.append(profile_id)
    return queued
