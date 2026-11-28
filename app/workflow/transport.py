"""EffectTransport for `task` and `calendar_event` effects.

Unexpected exceptions are deliberately not caught: EffectQueue.run_once records them as
`unknown`, which is right when a write may or may not have reached the provider."""

from __future__ import annotations

from pydantic import ValidationError

from collections.abc import Mapping, Sequence
from datetime import datetime

from app.calendar.busy import blocking_events, protected_overlap
from app.calendar.ports import (
    CalendarConflictError,
    CalendarPermanentError,
    CalendarPort,
    CalendarTemporaryError,
    EventSpec,
)
from app.contracts import DeliveryStatus, Effect, EffectKind, EffectResult
from app.workflow.tasks import TaskSink


class WorkflowTransport:
    def __init__(
        self, calendar: CalendarPort, tasks: TaskSink, *,
        protected: Mapping[str, Sequence[tuple[datetime, datetime]]] | None = None,
    ):
        """`protected` maps profile_id -> UTC intervals that timed events must not overlap."""
        self.calendar = calendar
        self.tasks = tasks
        self.protected = dict(protected or {})

    def execute(self, effect: Effect) -> EffectResult:
        if effect.kind == EffectKind.CALENDAR_EVENT:
            return self._calendar(effect)
        if effect.kind == EffectKind.TASK:
            return self._task(effect)
        return EffectResult(status=DeliveryStatus.FAILED, error_code="unsupported_kind")

    def _calendar(self, effect: Effect) -> EffectResult:
        try:
            event = EventSpec.model_validate(effect.payload["event"])
        except (KeyError, ValidationError):
            return EffectResult(status=DeliveryStatus.FAILED, error_code="invalid_payload")
        if event.uid != effect.target_ref:
            return EffectResult(status=DeliveryStatus.FAILED, error_code="invalid_payload")
        try:
            if event.starts_at is not None:
                if protected_overlap(self.protected.get(effect.profile_id, ()), event.starts_at, event.ends_at):
                    return EffectResult(status=DeliveryStatus.FAILED, error_code="protected_interval")
                busy = self.calendar.list_events(effect.profile_id, event.starts_at, event.ends_at)
                if blocking_events(busy, event.starts_at, event.ends_at, ignore_uid=event.uid):
                    return EffectResult(status=DeliveryStatus.FAILED, error_code="time_conflict")
            external_id = self.calendar.upsert_event(effect.profile_id, event)
        except CalendarConflictError as error:
            return EffectResult(status=DeliveryStatus.FAILED, error_code=error.code)
        except CalendarTemporaryError as error:
            return EffectResult(status=DeliveryStatus.RETRYABLE, error_code=error.code,
                                retry_after_seconds=error.retry_after_seconds)
        except CalendarPermanentError as error:
            return EffectResult(status=DeliveryStatus.FAILED, error_code=error.code)
        return EffectResult(status=DeliveryStatus.SENT, external_id=external_id[:128])

    def _task(self, effect: Effect) -> EffectResult:
        task = effect.payload.get("task")
        if not isinstance(task, dict):
            return EffectResult(status=DeliveryStatus.FAILED, error_code="invalid_payload")
        source = f"minutes:{effect.document_id}:v{effect.version}"
        external_id = self.tasks.publish(effect.profile_id, effect.key, task, source=source)
        return EffectResult(status=DeliveryStatus.SENT, external_id=external_id[:128])
