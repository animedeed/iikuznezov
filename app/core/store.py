from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from importlib.resources import files
from json import dumps
from pathlib import Path
from sqlite3 import Connection, Row, complete_statement, connect
from uuid import uuid4

from pydantic import TypeAdapter

from app.contracts import Candidate, DocumentVersion, MailDispatch, MailReceipt, Profile, ReceiptResult

_MIGRATIONS = ("001_initial.sql", "002_approvals.sql", "003_effects.sql")
_candidate_adapter = TypeAdapter(Candidate)


class BindingConflict(ValueError):
    """A mail connection is already assigned to another profile."""


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[Connection]:
        with closing(connect(self.path, timeout=5)) as db:
            db.row_factory = Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
        with self._connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(_MIGRATIONS):
                raise RuntimeError("Database schema is newer than this service")
            for number, name in enumerate(_MIGRATIONS[version:], start=version + 1):
                migration = files("migrations").joinpath(name).read_text(encoding="utf-8")
                self._apply_migration(db, migration)
                db.execute(f"PRAGMA user_version = {number}")

    @staticmethod
    def _apply_migration(db: Connection, sql: str) -> None:
        statement = ""
        for line in sql.splitlines(keepends=True):
            statement += line
            if complete_statement(statement):
                # The original migration declared its own transaction and schema version.
                if statement.strip().upper() not in {"BEGIN IMMEDIATE;", "COMMIT;", "PRAGMA USER_VERSION = 1;"}:
                    db.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete SQL migration")

    def seed_profiles(self, seeds: Sequence[tuple[Profile, str, int]]) -> None:
        with self._connection(write=True) as db:
            for profile, mail_channel, telegram_user_id in seeds:
                db.execute(
                    """INSERT INTO profiles VALUES (?, ?, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        timezone = excluded.timezone, quote_route = excluded.quote_route""",
                    (profile.profile_id, profile.timezone, profile.quote_route),
                )
                existing = db.execute(
                    "SELECT profile_id FROM bindings WHERE kind = 'mail' AND channel_id = ?",
                    (mail_channel,),
                ).fetchone()
                if existing is not None and existing["profile_id"] != profile.profile_id:
                    raise BindingConflict("Mail connection belongs to another profile")
                db.execute(
                    "DELETE FROM bindings WHERE kind = 'telegram' AND profile_id = ? AND channel_id <> ?",
                    (profile.profile_id, str(telegram_user_id)),
                )
                db.execute(
                    "UPDATE bindings SET active = 0 WHERE kind = 'mail' AND profile_id = ? AND channel_id <> ?",
                    (profile.profile_id, mail_channel),
                )
                db.executemany(
                    """INSERT INTO bindings (kind, channel_id, profile_id) VALUES (?, ?, ?)
                    ON CONFLICT (kind, channel_id, profile_id) DO UPDATE SET active = 1""",
                    (
                        ("mail", mail_channel, profile.profile_id),
                        ("telegram", str(telegram_user_id), profile.profile_id),
                    ),
                )

    def get_profile(self, profile_id: str) -> Profile | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT profile_id, timezone, quote_route FROM profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            return Profile.model_validate(dict(row)) if row is not None else None

    def receive_mail(self, channel_id: str, receipt: MailReceipt) -> ReceiptResult:
        external_key = dumps([channel_id, receipt.folder, receipt.uidvalidity, receipt.uid])
        with self._connection(write=True) as db:
            binding = db.execute(
                "SELECT profile_id FROM bindings WHERE kind = 'mail' AND channel_id = ? AND active = 1",
                (channel_id,),
            ).fetchone()
            if binding is None:
                raise LookupError("Unknown mail connection")
            profile_id = binding["profile_id"]
            event_id = uuid4().hex
            inserted = db.execute(
                """INSERT INTO inbox_events
                (profile_id, event_id, source, external_key, payload, received_at)
                VALUES (?, ?, 'mail', ?, ?, ?)
                ON CONFLICT (profile_id, source, external_key) DO NOTHING
                RETURNING event_id""",
                (profile_id, event_id, external_key, receipt.model_dump_json(by_alias=True),
                 receipt.received_at.isoformat()),
            ).fetchone()
            if inserted is not None:
                return ReceiptResult(event_id=event_id, profile_id=profile_id, duplicate=False)
            previous = db.execute(
                """SELECT event_id FROM inbox_events
                WHERE profile_id = ? AND source = 'mail' AND external_key = ?""",
                (profile_id, external_key),
            ).fetchone()
            return ReceiptResult(
                event_id=previous["event_id"], profile_id=profile_id, duplicate=True,
            )

    def get_mail(self, profile_id: str, event_id: str) -> MailReceipt | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT payload FROM inbox_events
                WHERE profile_id = ? AND event_id = ? AND source = 'mail'""",
                (profile_id, event_id),
            ).fetchone()
            return MailReceipt.model_validate_json(row["payload"]) if row is not None else None

    def record_draft(
        self, profile_id: str, source_id: str, candidate: Candidate, *,
        mail_dispatch: MailDispatch | None = None,
    ) -> DocumentVersion:
        """Persist a first draft without opening an approval."""
        with self._connection(write=True) as db:
            return self._record_draft(db, profile_id, source_id, candidate, mail_dispatch)

    @classmethod
    def _record_draft(
        cls, db: Connection, profile_id: str, source_id: str, candidate: Candidate,
        mail_dispatch: MailDispatch | None,
    ) -> DocumentVersion:
        origin = db.execute(
            "SELECT source FROM inbox_events WHERE profile_id = ? AND event_id = ?",
            (profile_id, source_id),
        ).fetchone()
        if origin is None:
            raise LookupError("Unknown source in this profile")
        expected_source = "mail" if candidate.kind == "mail_action" else "meeting"
        if origin["source"] != expected_source:
            raise ValueError("Draft kind does not match its source")
        previous = db.execute(
            "SELECT document_id FROM documents WHERE profile_id = ? AND source_event_id = ? AND kind = ?",
            (profile_id, source_id, candidate.kind),
        ).fetchone()
        if previous is not None:
            saved = cls._get_version(db, profile_id, previous["document_id"], 1)
            if saved is None:
                raise RuntimeError("Document has no original version")
            return saved
        document = DocumentVersion(
            profile_id=profile_id, document_id=uuid4().hex, source_id=source_id,
            version=1, candidate=candidate, mail_dispatch=mail_dispatch,
        )
        db.execute(
            """INSERT INTO documents (profile_id, document_id, source_event_id, kind)
            VALUES (?, ?, ?, ?)""",
            (profile_id, document.document_id, document.source_id, candidate.kind),
        )
        cls._insert_version(db, document)
        return document

    @staticmethod
    def _insert_version(db: Connection, document: DocumentVersion) -> None:
        db.execute(
            """INSERT INTO document_versions (profile_id, document_id, version, candidate, mail_dispatch)
            VALUES (?, ?, ?, ?, ?)""",
            (document.profile_id, document.document_id, document.version, document.candidate.model_dump_json(),
             document.mail_dispatch.model_dump_json() if document.mail_dispatch is not None else None),
        )

    def get_version(self, profile_id: str, document_id: str, version: int) -> DocumentVersion | None:
        with self._connection() as db:
            return self._get_version(db, profile_id, document_id, version)

    @staticmethod
    def _get_version(
        db: Connection, profile_id: str, document_id: str, version: int | None = None,
    ) -> DocumentVersion | None:
        row = db.execute(
            """SELECT d.source_event_id, v.candidate, v.mail_dispatch, v.version
            FROM document_versions AS v JOIN documents AS d USING (profile_id, document_id)
            WHERE v.profile_id = ? AND v.document_id = ? AND (? IS NULL OR v.version = ?)
            ORDER BY v.version DESC LIMIT 1""",
            (profile_id, document_id, version, version),
        ).fetchone()
        if row is None:
            return None
        return DocumentVersion(
            profile_id=profile_id, document_id=document_id, source_id=row["source_event_id"],
            version=row["version"], candidate=_candidate_adapter.validate_json(row["candidate"]),
            mail_dispatch=(MailDispatch.model_validate_json(row["mail_dispatch"])
                           if row["mail_dispatch"] is not None else None),
        )
