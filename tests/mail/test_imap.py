from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from app.contracts import Profile
from app.core.store import Store
from app.mail.imap import ImapSettings, MailboxGenerationChanged, MailCursor, MailPollError, poll_once


def mime(text: str, *, html: bool = False) -> bytes:
    message = EmailMessage()
    message["From"] = "Покупатель <buyer@example.invalid>"
    message["Subject"] = "Запрос КП"
    message.set_content(text, subtype="html" if html else "plain")
    return message.as_bytes()


class FakeMailbox:
    def __init__(self, messages, generation=42):
        self.messages = messages
        self.generation = generation
        self.calls = []

    def factory(self, host, port, timeout, ssl_context):
        assert (host, port, timeout) == ("imap.yandex.ru", 993, 20)
        assert ssl_context.check_hostname
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def login(self, username, password):
        assert (username, password) == ("reader@example.invalid", "test")
        return "OK", [b"connected"]

    def select(self, folder, readonly=False):
        assert folder == "INBOX" and readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, key):
        value = self.generation if key == "UIDVALIDITY" else max(self.messages, default=0) + 1
        return key, [str(value).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            assert args[:2] == (None, "UID")
            start, end = map(int, args[2].split(":"))
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages if start <= uid <= end)]
        uid, query = args
        raw = self.messages[int(uid)]
        if query == "(UID RFC822.SIZE INTERNALDATE)":
            return "OK", [f'1 (UID {uid} RFC822.SIZE {len(raw)} INTERNALDATE " 7-Dec-2026 10:00:00 +0300")'.encode()]
        assert query.startswith("(UID BODY.PEEK[]<0.")
        return "OK", [(f"1 (BODY[]<0> {{{len(raw)}}}".encode(), raw), f" UID {uid})".encode()]


def seed(store, channel, profile, actor):
    store.seed_profiles([(Profile(profile_id=profile, timezone="UTC", quote_route="forward_sales"), channel, actor)])


def settings(channel="box", **kwargs):
    return ImapSettings(channel, "reader@example.invalid", "test", **kwargs)


def test_first_start_at_tail_then_poll_with_profile_scoped_replay(tmp_path):
    store = Store(tmp_path / "mail.sqlite3")
    store.initialize()
    seed(store, "box", "p1", 1)
    seed(store, "box2", "p2", 2)
    mailbox = FakeMailbox({3: mime("Старое письмо")})
    baseline = poll_once(settings(), store.receive_mail, imap_factory=mailbox.factory)
    assert baseline.cursor == MailCursor(42, 3) and baseline.receipts == ()
    assert mailbox.calls == []
    mailbox.messages[4] = mime("<p>Новый запрос КП</p><script>Ignored</script><img src='image'>", html=True)
    result = poll_once(settings(), store.receive_mail, cursor=baseline.cursor, imap_factory=mailbox.factory)
    assert result.cursor == MailCursor(42, 4)
    entry, = result.receipts
    assert entry.profile_id == "p1" and not entry.duplicate
    mail = store.get_mail("p1", entry.event_id)
    assert (mail.sender, mail.subject, mail.text) == ("buyer@example.invalid", "Запрос КП", "Новый запрос КП")
    assert mail.received_at == datetime(2026, 12, 7, 7, tzinfo=UTC)
    assert store.get_mail("p2", entry.event_id) is None

    replay = poll_once(settings(), store.receive_mail, cursor=baseline.cursor, imap_factory=mailbox.factory)
    assert replay.receipts[0].event_id == entry.event_id and replay.receipts[0].duplicate
    second = poll_once(settings("box2"), store.receive_mail, initial_after_uid=3, imap_factory=mailbox.factory)
    assert second.receipts[0].profile_id == "p2" and second.receipts[0].event_id != entry.event_id


def test_generation_change_does_not_read_or_ingest():
    mailbox = FakeMailbox({1: mime("Повтор")})
    with pytest.raises(MailboxGenerationChanged) as error:
        poll_once(settings(), None, cursor=MailCursor(41, 1), imap_factory=mailbox.factory)
    assert error.value.current == MailCursor(42, 1)
    assert mailbox.calls == []


def test_batch_and_bad_mime_hold_cursor(tmp_path):
    store = Store(tmp_path / "mail.sqlite3")
    store.initialize()
    seed(store, "box", "p1", 1)
    mailbox = FakeMailbox({2: mime("Запрос КП"), 4: b"Subject: invalid\n\nmissing sender"})
    first = poll_once(settings(batch_size=1), store.receive_mail, initial_after_uid=0, imap_factory=mailbox.factory)
    assert first.cursor == MailCursor(42, 2)
    with pytest.raises(MailPollError, match="Cannot parse IMAP UID 4"):
        poll_once(settings(), store.receive_mail, cursor=first.cursor, imap_factory=mailbox.factory)
    mailbox.messages[4] = mime("Исправленная копия")
    retry = poll_once(settings(), store.receive_mail, cursor=first.cursor, imap_factory=mailbox.factory)
    assert retry.cursor == MailCursor(42, 4)
    assert len(retry.receipts) == 1


def test_large_message_does_not_fetch_body():
    mailbox = FakeMailbox({3: mime("Большое письмо")})
    with pytest.raises(MailPollError, match="exceeds message size"):
        poll_once(settings(max_message_bytes=10), None, initial_after_uid=0, imap_factory=mailbox.factory)
    assert len(mailbox.calls) == 2
