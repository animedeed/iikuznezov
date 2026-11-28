"""Bounded, read-only IMAP collection. Core owns persistence of the returned cursor."""

from __future__ import annotations

import imaplib
import math
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from app.contracts import MailReceipt, ReceiptResult


@dataclass(frozen=True)
class ImapSettings:
    channel_id: str
    username: str
    password: str = field(repr=False)
    host: str = "imap.yandex.ru"
    port: int = 993
    timeout_seconds: float = 20
    folder: str = "INBOX"
    batch_size: int = 25
    max_message_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not all((self.channel_id, self.username, self.password, self.host)):
            raise ValueError("IMAP credentials and channel are required")
        if self.folder != "INBOX":
            raise ValueError("Only INBOX is supported")
        if (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
                or self.batch_size <= 0 or self.max_message_bytes <= 0 or not 0 < self.port <= 65535):
            raise ValueError("IMAP limits must be positive")


@dataclass(frozen=True)
class MailCursor:
    uidvalidity: int
    uid: int

    def __post_init__(self) -> None:
        if self.uidvalidity <= 0 or self.uid < 0:
            raise ValueError("Invalid IMAP cursor")


@dataclass(frozen=True)
class PollResult:
    cursor: MailCursor
    receipts: tuple[ReceiptResult, ...]


class MailPollError(RuntimeError):
    """Polling stopped without advancing past the failed UID."""


class MailboxGenerationChanged(MailPollError):
    def __init__(self, current: MailCursor):
        super().__init__("IMAP UIDVALIDITY changed; reconcile the mailbox before resuming")
        self.current = current


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden is None and tag in {"script", "style", "head", "template"}:
            self.hidden = tag
        if self.hidden is None and tag in {"br", "div", "li", "p", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden == tag:
            self.hidden = None
        if self.hidden is None and tag in {"div", "li", "p", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden is None:
            self.parts.append(data)


def parse_message(
    raw: bytes, *, folder: str, uidvalidity: int, uid: int, received_at: datetime,
) -> MailReceipt:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        source = message["From"]
        if message.defects or source is None or source.defects or len(source.addresses) != 1:
            raise ValueError("Invalid MIME headers")
        sender = source.addresses[0]
        if not sender.username or not sender.domain:
            raise ValueError("Invalid MIME sender")
        body = message.get_body(preferencelist=("plain", "html"))
        if body is not None and body.defects:
            raise ValueError("Invalid MIME body")
        content = body.get_content() if body is not None else ""
        if body is not None and (body.defects or not isinstance(content, str)):
            raise ValueError("Invalid MIME body")
        if body is not None and body.get_content_type() == "text/html":
            parser = _PlainHTML()
            parser.feed(content)
            parser.close()
            content = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        if not content.strip():
            raise ValueError("Missing MIME body")
        return MailReceipt.model_validate({
            "folder": folder, "uidvalidity": uidvalidity, "uid": uid, "from": sender.addr_spec,
            "subject": str(message["Subject"] or "")[:998], "text": content[:100_000], "received_at": received_at,
        })
    except (ValueError, LookupError, TypeError, AttributeError, RecursionError):
        raise MailPollError(f"Cannot parse IMAP UID {uid}") from None


def _response_number(connection: imaplib.IMAP4_SSL, key: str) -> int:
    _, response = connection.response(key)
    value = response[-1] if response else None
    if not isinstance(value, bytes) or not value.isdigit() or int(value) <= 0:
        raise MailPollError(f"IMAP response lacks {key}")
    return int(value)


def _require_ok(response: tuple[str, list], command: str) -> list:
    status, data = response
    if status != "OK":
        raise MailPollError(f"IMAP {command} failed")
    return data


def _fetch_message(
    connection: imaplib.IMAP4_SSL, settings: ImapSettings, generation: int, uid: int,
) -> MailReceipt:
    uid_field = rb"\bUID " + str(uid).encode() + rb"\b"
    metadata = _require_ok(connection.uid("FETCH", str(uid), "(UID RFC822.SIZE INTERNALDATE)"), "metadata fetch")
    for item in metadata:
        if isinstance(item, bytes) and re.search(uid_field, item):
            size = re.search(rb"\bRFC822\.SIZE (\d+)\b", item)
            date = re.search(rb'\bINTERNALDATE "([^"]+)"', item)
            if size and date:
                break
    else:
        raise MailPollError(f"Missing metadata for IMAP UID {uid}")
    if int(size[1]) > settings.max_message_bytes:
        raise MailPollError(f"IMAP UID {uid} exceeds message size limit")
    try:
        received_at = parsedate_to_datetime(date[1].decode("ascii").replace("-", " ", 2))
    except (UnicodeError, ValueError):
        raise MailPollError(f"Invalid date for IMAP UID {uid}") from None
    # Partial fetch bounds memory even if the server's declared size is inaccurate.
    spec = f"(UID BODY.PEEK[]<0.{settings.max_message_bytes + 1}>)"
    data = _require_ok(connection.uid("FETCH", str(uid), spec), "body fetch")
    for index, item in enumerate(data):
        if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes)
                and re.search(rb"\bBODY\[\](?:<0>)? ", item[0]) and isinstance(item[1], bytes)):
            continue
        trailer = data[index + 1] if index + 1 < len(data) else b""
        attributes = item[0]
        if isinstance(trailer, bytes) and trailer.startswith(b" "):
            attributes += trailer
        if not re.search(uid_field, attributes):
            continue
        if len(item[1]) != int(size[1]):
            raise MailPollError(f"Incomplete IMAP message for UID {uid}")
        return parse_message(item[1], folder=settings.folder, uidvalidity=generation, uid=uid, received_at=received_at)
    raise MailPollError(f"Missing IMAP message for UID {uid}")


def poll_once(
    settings: ImapSettings, receive_mail: Callable[[str, MailReceipt], ReceiptResult], *,
    cursor: MailCursor | None = None, initial_after_uid: int | None = None,
    imap_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
) -> PollResult:
    """Persist up to one batch. By default an empty cursor starts at the current mailbox tail."""
    if initial_after_uid is not None and (initial_after_uid < 0 or cursor is not None):
        raise ValueError("Initial UID is only allowed without an existing cursor")
    try:
        with imap_factory(settings.host, settings.port, timeout=settings.timeout_seconds,
                          ssl_context=ssl.create_default_context()) as connection:
            _require_ok(connection.login(settings.username, settings.password), "login")
            _require_ok(connection.select(settings.folder, readonly=True), "select")
            current = MailCursor(_response_number(connection, "UIDVALIDITY"), _response_number(connection, "UIDNEXT") - 1)
            if cursor is not None and cursor.uidvalidity != current.uidvalidity:
                raise MailboxGenerationChanged(current)
            last_uid = cursor.uid if cursor is not None else initial_after_uid
            if last_uid is None:
                return PollResult(current, ())
            if last_uid > current.uid:
                raise MailPollError("IMAP cursor is ahead of the mailbox")
            if last_uid == current.uid:
                return PollResult(current, ())
            end_uid = min(last_uid + max(1000, settings.batch_size), current.uid)
            selection = f"{last_uid + 1}:{end_uid}"
            data = _require_ok(connection.uid("SEARCH", None, "UID", selection), "search")
            if len(data) != 1 or not isinstance(data[0], bytes) or any(not item.isdigit() for item in data[0].split()):
                raise MailPollError("Invalid IMAP search response")
            uids = sorted({int(item) for item in data[0].split() if last_uid < int(item) <= end_uid})
            receipts = []
            for uid in uids[:settings.batch_size]:
                receipt = _fetch_message(connection, settings, current.uidvalidity, uid)
                receipts.append(receive_mail(settings.channel_id, receipt))
            next_uid = uids[settings.batch_size - 1] if len(uids) > settings.batch_size else end_uid
            next_cursor = MailCursor(current.uidvalidity, next_uid)
            return PollResult(next_cursor, tuple(receipts))
    except (imaplib.IMAP4.error, OSError):
        raise MailPollError("IMAP connection failed") from None
