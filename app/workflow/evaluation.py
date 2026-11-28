"""Offline scoring of generated minutes against a reference summary, plus timing records.
The reference is never sent to a model: it only enters `evaluate_minutes`."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.contracts import MinutesCandidate
from app.core.store import Store

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(text.lower()) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.5) -> bool:
    x, y = _tokens(a), _tokens(b)
    return bool(x and y) and len(x & y) / len(x | y) >= threshold


@dataclass(frozen=True)
class MinutesScore:
    decision_recall: float
    decision_precision: float
    task_recall: float
    assignee_accuracy: float | None  # among matched tasks; None if nothing matched
    due_accuracy: float | None
    matched_tasks: int


def _ratio(hit: int, total: int, empty: float = 1.0) -> float:
    return hit / total if total else empty


def evaluate_minutes(candidate: MinutesCandidate, reference: Mapping[str, Sequence]) -> MinutesScore:
    """reference = {"decisions": [str], "tasks": [{"action", "assignee_ref", "due_date"}]}"""
    ref_decisions = list(reference.get("decisions", ()))
    got = [d.text for d in candidate.decisions]
    recalled = sum(any(_similar(r, g) for g in got) for r in ref_decisions)
    precise = sum(any(_similar(r, g) for r in ref_decisions) for g in got)

    ref_tasks, used, pairs = list(reference.get("tasks", ())), set(), []
    for ref in ref_tasks:
        for index, task in enumerate(candidate.tasks):
            if index not in used and _similar(ref["action"], task.action):
                used.add(index)
                pairs.append((ref, task))
                break
    assignee = sum(task.assignee_ref == ref.get("assignee_ref") for ref, task in pairs)
    due = sum((task.due_date.isoformat() if task.due_date else None) == ref.get("due_date") for ref, task in pairs)
    return MinutesScore(
        decision_recall=_ratio(recalled, len(ref_decisions)), decision_precision=_ratio(precise, len(got)),
        task_recall=_ratio(len(pairs), len(ref_tasks)),
        assignee_accuracy=assignee / len(pairs) if pairs else None,
        due_accuracy=due / len(pairs) if pairs else None, matched_tasks=len(pairs),
    )


def minutes_timings(store: Store, profile_id: str, document_id: str, version: int) -> dict[str, object]:
    """When the transcript arrived, the draft was opened and the initiator decided (UTC),
    with the gaps in seconds. Meeting end ~= transcript receipt in the current flow."""
    with store._connection() as db:
        row = db.execute(
            """SELECT e.received_at AS transcript_at, a.created_at AS draft_at, a.decided_at AS decided_at
            FROM approvals AS a JOIN documents AS d
              ON d.profile_id = a.profile_id AND d.document_id = a.document_id
            JOIN inbox_events AS e ON e.profile_id = d.profile_id AND e.event_id = d.source_event_id
            WHERE a.profile_id = ? AND a.document_id = ? AND a.version = ?""",
            (profile_id, document_id, version)).fetchone()
    if row is None:
        raise LookupError("Unknown approval in this profile")
    moments = {k: datetime.fromisoformat(row[k]) if row[k] else None
               for k in ("transcript_at", "draft_at", "decided_at")}

    def gap(a, b):
        return (moments[b] - moments[a]).total_seconds() if moments[a] and moments[b] else None

    return {**{k: v.isoformat() if v else None for k, v in moments.items()},
            "seconds_to_draft": gap("transcript_at", "draft_at"),
            "seconds_to_decision": gap("draft_at", "decided_at")}
