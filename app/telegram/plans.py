"""Telegram presentation of plans supplied by the workflow."""

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.contracts import Plan
from app.telegram.bot import TelegramAPI, _pages


Period = Literal["day", "week"]
_PERIODS: dict[str, Period] = {"/plan": "day", "/week": "week"}


def render_plan(plan: Plan, *, week_ends_sunday: bool = False) -> str:
    zone = ZoneInfo(plan.timezone)
    heading = f"План на день · {plan.date:%d.%m.%Y}"
    if plan.period == "week":
        end = plan.date + timedelta(days=6 - plan.date.weekday() if week_ends_sunday else 6)
        heading = f"План на неделю · {plan.date:%d.%m.%Y}–{end:%d.%m.%Y}"
    meetings = []
    for meeting in plan.meetings:
        when = (
            meeting.starts_at.astimezone(zone).strftime("%d.%m %H:%M")
            if meeting.starts_at else "время уточняется"
        )
        meetings.append(f"{when} — {meeting.title}")
    sections = (
        ("Встречи", meetings),
        ("Поручения к сроку", [f"{t.due_date:%d.%m} — {t.title}" for t in plan.tasks_due]),
        ("Просроченные поручения", [f"{t.due_date:%d.%m} — {t.title}" for t in plan.overdue]),
        (f"Итоги рабочего дня {plan.previous_workday:%d.%m}", plan.previous_workday_results),
        ("Статусы писем", plan.mail_statuses),
    )
    text = [f"{heading}\nЧасовой пояс: {plan.timezone}"]
    for title, values in sections:
        entries = "\n".join(f"• {value}" for value in values) or "Нет записей"
        text.append(f"{title}\n{entries}")
    if plan.pending_approvals:
        text.append(f"Ждут вашего решения: {len(plan.pending_approvals)}\nОткройте карточки согласования в чате.")
    return "\n\n".join(text)


class PlanCommands:
    def __init__(
        self, api: TelegramAPI, profiles_for: Callable[[int], Mapping[str, str]],
        load_plan: Callable[[str, date, Period], Plan], *, bot_username: str,
        week_ends_sunday: bool = False,
    ):
        self.api = api
        self.profiles_for = profiles_for
        self.load_plan = load_plan
        self.bot_username = bot_username.casefold()
        self.week_ends_sunday = week_ends_sunday

    def handle(self, update: dict) -> bool:
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return False
        args = message["text"].split()
        if not args:
            return False
        command, _, username = args[0].partition("@")
        period = _PERIODS.get(command)
        if period is None or (username and username.casefold() != self.bot_username):
            return False
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return True
        user_id = sender.get("id")
        if chat.get("type") != "private" or type(user_id) is not int or user_id <= 0 or chat.get("id") != user_id:
            return True
        reply = self._reply(user_id, command, period, args[1:])
        for page in _pages(reply):
            self.api.call("sendMessage", chat_id=user_id, text=page)
        return True

    def _reply(self, user_id: int, command: str, period: Period, args: list[str]) -> str:
        profiles = self.profiles_for(user_id)
        if not profiles:
            return "У вас нет доступа к планам."
        when = None
        if len(args) in (1, 2):
            try:
                parsed = date.fromisoformat(args[0])
                # Providers may need adjacent days to complete the requested period.
                if parsed.isoformat() == args[0] and 2 <= parsed.year <= 9998:
                    when = parsed
            except ValueError:
                pass
        if when is None:
            return f"Укажите дату в формате ГГГГ-ММ-ДД. Например, {command} 2026-12-07."
        if len(args) == 2:
            profile_id = args[1]
            if profile_id not in profiles:
                return "У вас нет доступа к этому профилю."
        elif len(profiles) > 1:
            choices = "\n".join(
                f"{label} — {command} {when.isoformat()} {key}" for key, label in profiles.items()
            )
            return f"Выберите профиль и отправьте одну из команд:\n{choices}"
        else:
            profile_id = next(iter(profiles))
        try:
            plan = self.load_plan(profile_id, when, period)
        except LookupError:
            return "План для этой даты пока недоступен. Повторите запрос позже."
        if (plan.profile_id, plan.date, plan.period) != (profile_id, when, period):
            return "Не удалось получить план для выбранной даты и профиля. Повторите запрос позже."
        return render_plan(plan, week_ends_sunday=self.week_ends_sunday)
