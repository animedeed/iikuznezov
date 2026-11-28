from contextlib import closing
from json import loads
from sqlite3 import connect

import httpx

from app.telegram.bot import TelegramAPI
from app.telegram.inbox import UpdateInbox


def test_inbox_persists_before_acknowledging_and_deduplicates_per_bot(tmp_path):
    offsets = []

    def respond(request):
        offsets.append(loads(request.content)["offset"])
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 10}, {"update_id": 10}]})

    path = tmp_path / "inbox.sqlite3"
    handled = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramAPI("test", client)
        UpdateInbox(path, api, bot_id=1).poll_once(handled.append)
        assert offsets == [None]
        assert not handled

        first_bot = UpdateInbox(path, api, bot_id=1)
        first_bot.poll_once(handled.append)
        assert handled == [{"update_id": 10}]
        first_bot.poll_once(handled.append)
        first_bot.poll_once(handled.append)
        assert offsets == [None, 11, 11]
        assert handled == [{"update_id": 10}]

        second_bot = UpdateInbox(path, api, bot_id=2)
        second_bot.poll_once(handled.append)
        second_bot.poll_once(handled.append)
        assert offsets[-1] is None
        assert handled == [{"update_id": 10}, {"update_id": 10}]

    with closing(connect(path)) as db:
        assert db.execute("SELECT body, state FROM telegram_updates").fetchall() == [(None, "done")] * 2


def test_inbox_imports_old_cursor_and_handles_malformed_pending_update(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    with closing(connect(path)) as db, db:
        db.execute("CREATE TABLE updates (id INTEGER PRIMARY KEY, body TEXT, state TEXT)")
        db.executemany("INSERT INTO updates VALUES (?, ?, ?)", [
            (8, None, "done"), (9, "invalid JSON", "pending"), (10, '{"update_id": 10}', "pending"),
        ])
    offsets, handled = [], []

    def respond(request):
        offsets.append(loads(request.content)["offset"])
        return httpx.Response(200, json={"ok": True, "result": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramAPI("test", client)
        inbox = UpdateInbox(path, api, bot_id=1, import_legacy=True)
        inbox.poll_once(handled.append)
        inbox = UpdateInbox(path, api, bot_id=1, import_legacy=True)
        inbox.poll_once(handled.append)

    assert handled == [{"update_id": 10}]
    assert offsets == [11]
    with closing(connect(path)) as db:
        assert db.execute(
            "SELECT body, state FROM telegram_updates WHERE update_id = 9",
        ).fetchone() == (None, "failed")
