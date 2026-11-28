from datetime import date

import pytest

from app.contracts import Profile, Route
from app.core.approvals import ApprovalService, MeetingSource, TranscriptTurn
from app.core.store import Store
from app.workflow.minutes import MinutesDraft

LEADER = 10001
INITIATOR = 20002


class StubModel:
    """Returns a fixed draft and records what it was asked."""

    def __init__(self, answer: dict):
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    def generate_json(self, schema, messages):
        self.calls.append(list(messages))
        return schema.model_validate(self.answer)

    def reply(self, messages):
        return "ok"


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "t.sqlite3")
    db.initialize()
    db.seed_profiles((
        (Profile(profile_id="p1", timezone="Europe/Moscow", quote_route=Route.FORWARD_SALES), "mail-1", LEADER),
        (Profile(profile_id="p2", timezone="Europe/Moscow", quote_route=Route.REVIEW), "mail-2", 10002),
    ))
    return db


@pytest.fixture
def approvals(store):
    return ApprovalService(store)


def make_source(**over) -> MeetingSource:
    data = dict(
        meeting_id="m1", initiator={"telegram_user_id": INITIATOR},
        participant_refs=("meeting-initiator", "sales-desk"), allowed_assignee_refs=("sales-desk",),
        turns=(TranscriptTurn(request_id="turn-1", sequence=1, text="В четверг отправим КП."),
               TranscriptTurn(request_id="turn-2", sequence=2, text="Кто подтвердит дату?")),
    )
    data.update(over)
    return MeetingSource.model_validate(data)


GOOD_DRAFT = {
    "title": "Встреча по поставке", "summary": "Нужен черновик КП.",
    "decisions": [{"text": "Подготовить КП.", "source_turn_ids": ["turn-1", "ghost"]}],
    "open_questions": [{"text": "Кто подтвердит дату?", "source_turn_ids": ["turn-2"]}],
    "tasks": [
        {"action": "Подготовить черновик КП", "assignee_ref": "sales-desk", "due_date": "2026-12-10",
         "source_turn_ids": ["turn-1"]},
        {"action": "Подтвердить дату", "assignee_ref": None, "due_date": None, "source_turn_ids": []},
    ],
}


@pytest.fixture
def ids():
    from types import SimpleNamespace
    return SimpleNamespace(leader=LEADER, initiator=INITIATOR)


@pytest.fixture
def source_factory():
    return make_source


@pytest.fixture
def draft():
    from copy import deepcopy
    return deepcopy(GOOD_DRAFT)


@pytest.fixture
def model_factory():
    return StubModel
