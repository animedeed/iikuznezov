"""Internal task list (open/done with a source). Own table, created on first use, so the
shared migrations stay untouched. Rows are written only by the effect queue after approval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from pydantic import ValidationError

from app.contracts import Contract, Ref, ShortText, TaskDraft
from app.core.store import Store


class TaskInfo(Contract):
    id: Ref
    title: ShortText
    due_date: date
    assignee_ref: Ref | None = None
    source: Ref = "manual"


class TaskSource(Protocol):
    def list_open(self, profile_id: str) -> Sequence[TaskInfo]: ...


class TaskSink(Protocol):
    def publish(self, profile_id: str, key: str, task: Mapping[str, object], *, source: str) -> str:
        """Create the task once per key. Returns the task id."""
        ...


_SCHEMA = """CREATE TABLE IF NOT EXISTS workflow_tasks (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    assignee_ref TEXT,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT,
    PRIMARY KEY (profile_id, task_id),
    UNIQUE (profile_id, key)
)"""


class SqliteTaskStore:
    """Implements both TaskSink and TaskSource on the shared SQLite file."""

    def __init__(self, store: Store):
        self.store = store
        with store._connection(write=True) as db:
            db.execute(_SCHEMA)

    def publish(self, profile_id: str, key: str, task: Mapping[str, object], *, source: str) -> str:
        try:
            draft = TaskDraft.model_validate(dict(task))
        except ValidationError as error:
            raise ValueError("Task payload is invalid") from error
        if draft.status != "draft" or draft.due_date is None or draft.assignee_ref is None:
            raise ValueError("Only complete tasks can be created")
        with self.store._connection(write=True) as db:
            row = db.execute("SELECT task_id FROM workflow_tasks WHERE profile_id = ? AND key = ?",
                             (profile_id, key)).fetchone()
            if row is not None:
                return row["task_id"]
            task_id = f"{source}:{draft.task_ref}"[:128]
            db.execute(
                "INSERT INTO workflow_tasks (profile_id, task_id, key, title, assignee_ref, due_date, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile_id, task_id, key, draft.action, draft.assignee_ref, draft.due_date.isoformat(),
                 source, datetime.now(UTC).isoformat()),
            )
            return task_id

    def list_open(self, profile_id: str) -> Sequence[TaskInfo]:
        with self.store._connection() as db:
            rows = db.execute(
                "SELECT task_id, title, assignee_ref, due_date, source FROM workflow_tasks "
                "WHERE profile_id = ? AND status = 'open' ORDER BY due_date, task_id", (profile_id,)).fetchall()
        return [TaskInfo(id=r["task_id"], title=r["title"], due_date=date.fromisoformat(r["due_date"]),
                         assignee_ref=r["assignee_ref"], source=r["source"]) for r in rows]

    def mark_done(self, profile_id: str, task_id: str) -> bool:
        """True if the task was open. Scoped by profile, so other profiles cannot close it."""
        with self.store._connection(write=True) as db:
            return db.execute(
                "UPDATE workflow_tasks SET status = 'done', done_at = ? "
                "WHERE profile_id = ? AND task_id = ? AND status = 'open'",
                (datetime.now(UTC).isoformat(), profile_id, task_id)).rowcount == 1


class FakeTaskSink:
    def __init__(self):
        self.created: dict[tuple[str, str], dict[str, object]] = {}
        self.calls = 0

    def publish(self, profile_id: str, key: str, task: Mapping[str, object], *, source: str) -> str:
        self.calls += 1
        self.created.setdefault((profile_id, key), dict(task))
        return f"task-{len(self.created)}"
