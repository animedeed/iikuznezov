"""Enqueue and deliver one daily plan per profile and local date."""

from collections.abc import Callable
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from app.contracts import DeliveryStatus, Effect, EffectKind, EffectResult, Plan
from app.core.effects import EffectQueue, enqueue_effect
from app.core.store import Store
from app.telegram.bot import TelegramAPI, _pages, deliver_pages
from app.telegram.plans import Period, render_plan


class MorningPlans:
    def __init__(
        self, store: Store, api: TelegramAPI, profile_id: str, user_id: int, timezone: str,
        load_plan: Callable[[str, date, Period], Plan],
    ):
        self.store = store
        self.queue = EffectQueue(store)
        self.api = api
        self.profile_id = profile_id
        self.user_id = user_id
        self.timezone = ZoneInfo(timezone)
        self.load_plan = load_plan

    def tick(self, now: datetime) -> None:
        local = now.astimezone(self.timezone)
        if (local.hour, local.minute) == (8, 30):
            self.enqueue(local.date())
        self.drain(local.date())

    def enqueue(self, when: date) -> str:
        key = f"scheduled-plan:day:{when.isoformat()}"
        effect_id = uuid5(NAMESPACE_URL, f"iikuznezov:{self.profile_id}:{key}").hex
        if self.queue.status(effect_id) is not None:
            return effect_id
        plan = self.load_plan(self.profile_id, when, "day")
        if (plan.profile_id, plan.date, plan.period, plan.timezone) != (
            self.profile_id, when, "day", self.timezone.key,
        ):
            raise ValueError("Workflow returned a different plan than requested")
        effect = Effect(
            effect_id=effect_id, profile_id=self.profile_id, key=key, kind=EffectKind.TELEGRAM,
            target_ref=str(self.user_id),
            payload={"operation": "morning_plan", "date": when.isoformat(), "text": render_plan(plan)},
        )
        with self.store._connection(write=True) as db:
            exists = db.execute("SELECT 1 FROM effects WHERE effect_id = ?", (effect_id,)).fetchone()
            if exists is None:
                enqueue_effect(db, effect)
        return effect_id

    def drain(self, when: date) -> None:
        def execute(effect: Effect) -> EffectResult:
            if (
                effect.profile_id != self.profile_id or effect.kind != EffectKind.TELEGRAM
                or effect.target_ref != str(self.user_id) or effect.payload.get("operation") != "morning_plan"
                or effect.payload.get("date") != when.isoformat()
            ):
                return EffectResult(status=DeliveryStatus.FAILED, error_code="plan_expired_or_unbound")
            return deliver_pages(self.api, self.user_id, _pages(effect.payload["text"]))

        while self.queue.run_once(_PlanTransport(execute)) is not None:
            pass


class _PlanTransport:
    def __init__(self, execute: Callable[[Effect], EffectResult]):
        self.execute = execute
