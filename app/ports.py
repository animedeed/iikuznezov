"""Integration seams. These protocols do not perform I/O."""

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.contracts import Decision, DecisionStatus, Effect, EffectResult, TelegramActor

T = TypeVar("T", bound=BaseModel)


class ModelPort(Protocol):
    def generate_json(self, schema: type[T], messages: Sequence[dict[str, str]]) -> T: ...
    def reply(self, messages: Sequence[dict[str, str]]) -> str: ...


class EffectTransport(Protocol):
    def execute(self, effect: Effect) -> EffectResult: ...


class ApprovalPort(Protocol):
    def decide(
        self,
        approval_id: str,
        actor: TelegramActor,
        decision: Decision,
        comment: str | None = None,
    ) -> DecisionStatus: ...
