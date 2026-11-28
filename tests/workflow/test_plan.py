from datetime import UTC, date, datetime, time

import pytest

from app.calendar.fake import FakeCalendar
from app.calendar.ports import CalendarEvent, CalendarTemporaryError
from app.contracts import Decision, Effect, EffectKind, TelegramActor
from app.core.effects import enqueue_effect
from app.workflow.plan import build_plan, format_plan, previous_workday, schedule_plan_message, send_due_plans
from app.workflow.tasks import SqliteTaskStore, TaskInfo
from app.workflow.followups import process_meeting_event


class Tasks:
    def __init__(self, *items):
        self.items = items

    def list_open(self, profile_id):
        return self.items if profile_id == "p1" else ()


def ev(id, title, hour, day=7, all_day=False):
    start = datetime(2026, 12, day, hour, tzinfo=UTC)  # 12:00 MSK == 09:00 UTC
    return CalendarEvent(id=id, title=title, starts_at=start, all_day=all_day)


def test_previous_workday_skips_weekend():
    assert previous_workday(date(2026, 12, 7)) == date(2026, 12, 4)   # Monday -> Friday
    assert previous_workday(date(2026, 12, 8)) == date(2026, 12, 7)
    assert previous_workday(date(2026, 12, 7), {0, 1, 2, 3, 4, 5}) == date(2026, 12, 5)


def test_day_plan_uses_profile_timezone_and_ignores_other_profiles(store):
    cal = FakeCalendar({"p1": [
        ev("a", "Планёрка", 7),                      # 10:00 MSK on the 7th
        ev("late", "Поздняя", 21),                   # 00:00 MSK on the 8th: next day
        ev("early", "Ранняя", 20, day=6),            # 23:00 MSK on the 6th: previous day
        ev("ad", "Срок", 0, all_day=True),
    ], "p2": [ev("x", "Чужая", 7)]})
    tasks = Tasks(TaskInfo(id="t1", title="Прогноз", due_date=date(2026, 12, 7)),
                  TaskInfo(id="t0", title="Старое", due_date=date(2026, 12, 3)),
                  TaskInfo(id="t2", title="Позже", due_date=date(2026, 12, 9)))
    plan = build_plan("p1", date(2026, 12, 7), "day", store=store, calendar=cal, tasks=tasks)
    assert [m.id for m in plan.meetings] == ["a"]
    assert [t.id for t in plan.tasks_due] == ["t1"] and [t.id for t in plan.overdue] == ["t0"]
    assert plan.previous_workday == date(2026, 12, 4) and plan.timezone == "Europe/Moscow"


def test_week_plan_runs_to_end_of_iso_week(store):
    cal = FakeCalendar({"p1": [ev("w", "Среда", 9, day=9), ev("n", "Следующая", 9, day=14)]})
    tasks = Tasks(TaskInfo(id="t", title="Пт", due_date=date(2026, 12, 11)),
                  TaskInfo(id="u", title="Пн", due_date=date(2026, 12, 14)))
    plan = build_plan("p1", date(2026, 12, 7), "week", store=store, calendar=cal, tasks=tasks)
    assert [m.id for m in plan.meetings] == ["w"] and [t.id for t in plan.tasks_due] == ["t"]


def test_calendar_failure_is_not_hidden(store):
    cal = FakeCalendar()
    cal.fail_next(CalendarTemporaryError())
    with pytest.raises(CalendarTemporaryError):
        build_plan("p1", date(2026, 12, 7), store=store, calendar=cal, tasks=Tasks())


def test_unknown_profile_and_bad_period(store):
    with pytest.raises(LookupError):
        build_plan("nope", date(2026, 12, 7), store=store, calendar=FakeCalendar(), tasks=Tasks())
    with pytest.raises(ValueError):
        build_plan("p1", date(2026, 12, 7), "month", store=store, calendar=FakeCalendar(), tasks=Tasks())


def test_pending_approvals_results_and_mail_statuses(store, approvals, source_factory, model_factory, draft):
    event_id = approvals.submit_transcript("p1", source_factory())
    card = process_meeting_event(store, approvals, model_factory(draft), "p1", event_id)
    with store._connection(write=True) as db:  # a delivered email on the previous workday, one broken
        for n, status in enumerate(("sent", "unknown")):
            enqueue_effect(db, Effect(effect_id=f"e{n}", profile_id="p1", key=f"k{n}",
                                      kind=EffectKind.EMAIL, target_ref="sales-desk"))
        db.execute("UPDATE effects SET status='sent', updated_at='2026-12-04T09:00:00+00:00' WHERE effect_id='e0'")
        db.execute("UPDATE effects SET status='unknown' WHERE effect_id='e1'")
    plan = build_plan("p1", date(2026, 12, 7), store=store, calendar=FakeCalendar(), tasks=Tasks())
    assert plan.pending_approvals == (card.request.approval_id,)
    assert "Отправлено писем: 1" in plan.previous_workday_results
    assert any("неизвестным исходом" in s for s in plan.mail_statuses)
    other = build_plan("p2", date(2026, 12, 7), store=store, calendar=FakeCalendar(), tasks=Tasks())
    assert other.pending_approvals == () and other.previous_workday_results == () and other.mail_statuses == ()


def test_empty_window_gives_valid_empty_plan(store):
    plan = build_plan("p2", date(2026, 12, 7), "week", store=store, calendar=FakeCalendar(), tasks=Tasks())
    assert plan.meetings == () and plan.tasks_due == () and plan.overdue == ()
    assert "Встречи и сроки" in format_plan(plan)


def test_week_text_is_grouped_by_day(store):
    cal = FakeCalendar({"p1": [ev("w", "Среда", 9, day=9)]})
    plan = build_plan("p1", date(2026, 12, 7), "week", store=store, calendar=cal,
                      tasks=Tasks(TaskInfo(id="t", title="Отчёт", due_date=date(2026, 12, 11))))
    text = format_plan(plan)
    assert "Ср 09.12:\n• 12:00 Среда" in text and "Пт 11.12:\n• срок: Отчёт" in text


def test_plan_message_is_queued_once_per_day(store):
    plan = build_plan("p1", date(2026, 12, 7), store=store, calendar=FakeCalendar({"p1": [ev("a", "Планёрка", 7)]}),
                      tasks=Tasks())
    assert "Планёрка" in format_plan(plan) and "10:00" in format_plan(plan)
    assert schedule_plan_message(store, plan) is True and schedule_plan_message(store, plan) is False
    with store._connection() as db:
        row = db.execute("SELECT kind, payload FROM effects WHERE key LIKE 'plan:%'").fetchone()
    assert row["kind"] == "telegram" and '"plan_message"' in row["payload"]


def test_send_due_plans_respects_local_time_and_workdays(store):
    kw = dict(store=store, calendar=FakeCalendar(), tasks=Tasks())
    assert send_due_plans(datetime(2026, 12, 7, 5, 29, tzinfo=UTC), **kw) == []        # 08:29 MSK
    assert send_due_plans(datetime(2026, 12, 7, 5, 30, tzinfo=UTC), **kw) == ["p1", "p2"]
    assert send_due_plans(datetime(2026, 12, 7, 9, 0, tzinfo=UTC), **kw) == []         # already queued
    assert send_due_plans(datetime(2026, 12, 12, 9, 0, tzinfo=UTC), **kw) == []        # Saturday


def test_due_plan_skips_unselected_profiles_and_does_not_reread_delivered_calendar(store):
    class CountingCalendar(FakeCalendar):
        reads = []

        def list_events(self, profile_id, start, end):
            self.reads.append(profile_id)
            return super().list_events(profile_id, start, end)

    calendar = CountingCalendar()
    now = datetime(2026, 12, 7, 5, 30, tzinfo=UTC)
    kwargs = dict(store=store, calendar=calendar, tasks=Tasks(), profile_ids=("p1",))
    assert send_due_plans(now, **kwargs) == ["p1"]
    assert send_due_plans(now, **kwargs) == []
    assert calendar.reads == ["p1"]
