"""Scripted adapters for module tests. They never contact a provider."""

from collections.abc import Sequence
from pydantic import BaseModel

from app.contracts import Decision, DecisionStatus, Effect, EffectResult, TelegramActor
from app.ports import T


class FakeModel:
    def __init__(
        self, *, answer: BaseModel | Exception | None = None, text: str | Exception | None = None,
    ):
        self.answer = answer
        self.text = text
        self.calls: list[tuple[str, tuple[dict[str, str], ...]]] = []

    def generate_json(self, schema: type[T], messages: Sequence[dict[str, str]]) -> T:
        self.calls.append(("generate_json", tuple(dict(message) for message in messages)))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is None:
            raise RuntimeError("Set the fake model's JSON response explicitly")
        return schema.model_validate(self.answer.model_dump())

    def reply(self, messages: Sequence[dict[str, str]]) -> str:
        self.calls.append(("reply", tuple(dict(message) for message in messages)))
        if isinstance(self.text, Exception):
            raise self.text
        if self.text is None:
            raise RuntimeError("Set the fake model's text response explicitly")
        return self.text


class FakeTransport:
    def __init__(self, result: EffectResult):
        self.result = result
        self.calls: list[Effect] = []

    def execute(self, effect: Effect) -> EffectResult:
        self.calls.append(effect)
        return self.result


class FakeApprovals:
    def __init__(self, status: DecisionStatus):
        self.status = status
        self.calls: list[tuple[str, TelegramActor, Decision, str | None]] = []

    def decide(
        self, approval_id: str, actor: TelegramActor, decision: Decision, comment: str | None = None,
    ) -> DecisionStatus:
        self.calls.append((approval_id, actor, decision, comment))
        return self.status
