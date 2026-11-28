"""Process stored meeting transcripts using the workflow module."""

from app.adapters.model import ModelAdapterError
from app.contracts import DocumentVersion
from app.core.approvals import ApprovalService
from app.core.store import Store
from app.ports import ModelPort
from app.workflow.followups import process_meeting_event


def _saved_version(store: Store, profile_id: str, event_id: str) -> DocumentVersion | None:
    with store._connection() as db:
        row = db.execute(
            """SELECT d.document_id, a.version FROM documents AS d JOIN approvals AS a
               ON a.profile_id = d.profile_id AND a.document_id = d.document_id
               WHERE d.profile_id = ? AND d.source_event_id = ? AND d.kind = 'minutes'
               ORDER BY a.version DESC LIMIT 1""",
            (profile_id, event_id),
        ).fetchone()
    return store.get_version(profile_id, row["document_id"], row["version"]) if row is not None else None


def process_saved_meeting(
    store: Store, model: ModelPort, profile_id: str, event_id: str,
) -> DocumentVersion:
    """Persist a draft or recover the existing version on replay, including after approval."""
    saved = _saved_version(store, profile_id, event_id)
    if saved is not None:
        return saved
    try:
        return process_meeting_event(store, ApprovalService(store), model, profile_id, event_id).document
    except ValueError as error:
        # A concurrent operator may have closed the first approval while the model was running.
        if str(error) == "Current approval is closed":
            saved = _saved_version(store, profile_id, event_id)
            if saved is not None:
                return saved
        raise


def process_pending_meetings(store: Store, model: ModelPort, *, profile_id: str) -> dict:
    """Run one controlled batch. Failed events stay available for the next run."""
    if store.get_profile(profile_id) is None:
        raise LookupError("Unknown profile")
    with store._connection() as db:
        rows = db.execute(
            """SELECT event_id FROM inbox_events
               WHERE source = 'meeting' AND status = 'received' AND profile_id = ?
               ORDER BY received_at, event_id""",
            (profile_id,),
        ).fetchall()
    processed = []
    errors = []
    for row in rows:
        event_id = row["event_id"]
        try:
            version = process_saved_meeting(store, model, profile_id, event_id)
        except ModelAdapterError as error:
            errors.append({"event_id": event_id, "error_code": error.code})
        except ValueError:
            errors.append({"event_id": event_id, "error_code": "invalid_meeting_draft"})
        else:
            processed.append({"event_id": event_id, "document_id": version.document_id, "version": version.version})
    return {"processed": processed, "errors": errors}

