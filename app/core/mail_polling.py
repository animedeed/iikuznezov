"""Persist incoming mail before classifying and advancing its IMAP cursor."""

from collections.abc import Mapping

from app.adapters.model import ModelAdapterError
from app.contracts import MailCandidate, MailReceipt, Route
from app.core.mail_intake import process_mail
from app.core.store import Store
from app.mail.imap import ImapSettings, MailCursor, poll_once
from app.mail.routing import MailRoutingContext, classify_mail, route_mail
from app.ports import ModelPort


class MailPolling:
    def __init__(
        self, store: Store, imap: ImapSettings, routing: MailRoutingContext, *,
        recipient_fingerprints: Mapping[str, str], model: ModelPort | None,
    ):
        self.store = store
        self.imap = imap
        self.routing = routing
        self.fingerprints = recipient_fingerprints
        self.model = model
        with store._connection(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS imap_cursors (
                    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
                    channel_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    PRIMARY KEY (profile_id, channel_id, folder)
                )
            """)

    def tick(self) -> None:
        profile_id = self.routing.profile.profile_id
        channel_id = self.imap.channel_id
        key = (profile_id, channel_id, self.imap.folder)
        with self.store._connection() as db:
            binding = db.execute(
                "SELECT 1 FROM bindings WHERE profile_id=? AND channel_id=? AND kind='mail' AND active=1",
                (profile_id, channel_id),
            ).fetchone()
            saved = db.execute(
                "SELECT uidvalidity, uid FROM imap_cursors WHERE profile_id=? AND channel_id=? AND folder=?",
                key,
            ).fetchone()
        if binding is None:
            raise PermissionError("Mail channel is not bound to the profile")
        self._process_pending()
        cursor = None if saved is None else MailCursor(saved["uidvalidity"], saved["uid"])
        result = poll_once(self.imap, self.store.receive_mail, cursor=cursor)
        with self.store._connection(write=True) as db:
            db.execute(
                """INSERT INTO imap_cursors VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (profile_id, channel_id, folder) DO UPDATE SET uid=excluded.uid
                WHERE uidvalidity=excluded.uidvalidity AND uid < excluded.uid""",
                (*key, result.cursor.uidvalidity, result.cursor.uid),
            )
        self._process_pending()

    def _process_pending(self) -> None:
        profile_id = self.routing.profile.profile_id
        with self.store._connection() as db:
            rows = db.execute(
                """SELECT event_id, payload FROM inbox_events
                WHERE profile_id=? AND source='mail' AND status='received'
                AND json_extract(external_key, '$[0]')=?
                ORDER BY received_at, event_id LIMIT 25""",
                (profile_id, self.imap.channel_id),
            ).fetchall()
        for row in rows:
            receipt = MailReceipt.model_validate_json(row["payload"])
            try:
                candidate = classify_mail(receipt, self.routing, model=self.model)
            except ModelAdapterError:
                candidate = MailCandidate(
                    topic="unknown", requires_review=True, rationale="Классификация требует проверки.",
                )
            routed = route_mail(candidate, self.routing)
            if routed.route in {Route.IGNORE, Route.RECORD_STATUS}:
                with self.store._connection(write=True) as db:
                    db.execute(
                        "UPDATE inbox_events SET status=? WHERE profile_id=? AND event_id=? AND status='received'",
                        (routed.route.value, profile_id, row["event_id"]),
                    )
                continue
            alias = routed.target_ref or self.routing.targets.get(Route.REVIEW)
            fingerprint = self.fingerprints.get(alias) if alias else None
            trusted_quote = routed.automatic and routed.route == Route.FORWARD_SALES and candidate.topic == "quote"
            if trusted_quote:
                candidate = candidate.model_copy(update={"topic": "quote_request"})
            process_mail(
                self.store, self.imap.channel_id, receipt, candidate,
                sales_alias=alias if fingerprint else None,
                trusted_quote=trusted_quote and bool(fingerprint),
                recipient_fingerprint=fingerprint,
            )
