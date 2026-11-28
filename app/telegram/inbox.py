"""Durable Bot API update intake for a single polling worker per bot."""

from collections.abc import Callable
from contextlib import closing
from json import dumps, loads
from logging import getLogger
from pathlib import Path
from sqlite3 import Connection, connect
from time import sleep

from app.telegram.bot import TelegramAPI, TelegramError


logger = getLogger(__name__)


class UpdateInbox:
    def __init__(self, path: Path, api: TelegramAPI, bot_id: int, *, import_legacy: bool = False):
        self.path = path
        self.api = api
        self.bot_id = bot_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_updates (
                bot_id INTEGER NOT NULL, update_id INTEGER NOT NULL, body TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (bot_id, update_id)
            )""")
            if import_legacy and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'updates'"
            ).fetchone():
                db.execute(
                    """INSERT OR IGNORE INTO telegram_updates (bot_id, update_id, body, state)
                       SELECT ?, id, body, state FROM updates""",
                    (bot_id,),
                )
                db.execute("DROP TABLE updates")

    def poll_once(self, handle: Callable[[dict], None]) -> None:
        """Process durable pending updates, or fetch and persist the next batch."""
        with closing(connect(self.path)) as db:
            pending = db.execute(
                """SELECT update_id, body FROM telegram_updates
                   WHERE bot_id = ? AND state = 'pending' ORDER BY update_id""",
                (self.bot_id,),
            ).fetchall()
            if not pending:
                self._fetch(db)
                return
            for update_id, body in pending:
                state = "done"
                delay = 0
                try:
                    update = loads(body)
                    if not isinstance(update, dict):
                        raise ValueError("Expected an update object")
                    handle(update)
                except TelegramError as error:
                    state = "failed"
                    delay = error.retry_after or (2 if error.code == 429 else 0)
                    logger.warning("Telegram update %s: %s", update_id, error)
                except (KeyError, TypeError, ValueError):
                    state = "failed"
                    logger.warning("Invalid Telegram update %s", update_id)
                with db:
                    db.execute(
                        """UPDATE telegram_updates SET state = ?, body = NULL
                           WHERE bot_id = ? AND update_id = ?""",
                        (state, self.bot_id, update_id),
                    )
                if delay:
                    sleep(delay)

    def _fetch(self, db: Connection) -> None:
        cursor = db.execute(
            "SELECT MAX(update_id) FROM telegram_updates WHERE bot_id = ?", (self.bot_id,),
        ).fetchone()[0]
        try:
            updates = self.api.updates(cursor + 1 if cursor is not None else None)
        except TelegramError as error:
            if error.code in (401, 404, 409):
                raise
            sleep(max(2, error.retry_after))
            return
        with db:
            db.executemany(
                "INSERT OR IGNORE INTO telegram_updates (bot_id, update_id, body) VALUES (?, ?, ?)",
                [(self.bot_id, update["update_id"], dumps(update)) for update in updates
                 if isinstance(update, dict) and type(update.get("update_id")) is int
                 and update["update_id"] >= 0],
            )
