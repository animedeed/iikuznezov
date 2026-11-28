"""SMTP delivery for email effects. Implements `EffectTransport`; no I/O at import time."""

from __future__ import annotations

import smtplib
import ssl
from hashlib import sha256
from collections.abc import Callable
from email.message import EmailMessage

from app.contracts import DeliveryStatus, Effect, EffectKind, EffectResult
from app.core.store import Store
from app.mail.config import SmtpSettings, valid_address

SmtpFactory = Callable[..., smtplib.SMTP]


def _result(status: DeliveryStatus, code: str | None = None, external_id: str | None = None) -> EffectResult:
    return EffectResult(status=status, error_code=code, external_id=external_id)


def recipient_fingerprint(address: str) -> str:
    """Pin a trusted recipient address without placing it in the effect payload."""
    normalized = valid_address(address)
    if normalized is None:
        raise ValueError("Invalid SMTP recipient address")
    return sha256(normalized.encode("ascii")).hexdigest()


class _TrackedSMTP(smtplib.SMTP_SSL):
    """Record whether the provider invited the client to start sending DATA."""

    _inside_data = False
    data_started = False

    def data(self, msg):
        self._inside_data = True
        self.data_started = False
        try:
            return super().data(msg)
        finally:
            self._inside_data = False

    def getreply(self):
        code, response = super().getreply()
        if self._inside_data and code == 354:
            self.data_started = True
        return code, response


def _one_line(value: str, limit: int = 200) -> str:
    return " ".join(value.split())[:limit]


def _minutes_body(minutes: dict[str, object]) -> tuple[str, str]:
    def lines(items: object) -> list[str]:
        return [f"- {item['text']}" for item in items or [] if isinstance(item, dict) and "text" in item]

    tasks = [
        f"- {t.get('action', '')}" + (f" (срок {t['due_date']})" if t.get("due_date") else "")
        for t in minutes.get("tasks") or [] if isinstance(t, dict)
    ]
    parts = [str(minutes.get("summary", ""))]
    for title, items in (("Решения", lines(minutes.get("decisions"))), ("Открытые вопросы", lines(minutes.get("open_questions"))),
                         ("Задачи", tasks)):
        if items:
            parts.append(f"{title}:\n" + "\n".join(items))
    return f"Протокол: {_one_line(str(minutes.get('title', 'встреча')))}", "\n\n".join(parts)


class SmtpEmailTransport:
    """Sends one email effect to one recipient, with a stable Message-ID per effect.

    Outcome mapping (see docs/integrations/mail.md): refusal before the message body was
    accepted is retryable/failed; anything ambiguous after the body started is `unknown`.
    """

    def __init__(
        self, settings: SmtpSettings, *, store: Store | None = None,
        smtp_factory: SmtpFactory = _TrackedSMTP, require_recipient_fingerprint: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.smtp_factory = smtp_factory
        self.require_recipient_fingerprint = require_recipient_fingerprint

    # -- message building ---------------------------------------------------
    def _recipient(self, effect: Effect) -> str | None:
        address = self.settings.recipients.get(effect.target_ref)
        return valid_address(address) if address else None

    def _was_approved(self, effect: Effect) -> bool:
        if self.store is None or effect.document_id is None:
            return False
        with self.store._connection() as db:
            return db.execute(
                """SELECT 1 FROM approvals WHERE profile_id = ? AND document_id = ? AND version = ?
                   AND state = 'approved' AND decision = 'approve'""",
                (effect.profile_id, effect.document_id, effect.version),
            ).fetchone() is not None

    def build_message(self, effect: Effect, recipient: str) -> EmailMessage | EffectResult:
        payload = effect.payload
        if "minutes" in payload and isinstance(payload["minutes"], dict):
            subject, body = _minutes_body(payload["minutes"])
        else:
            action = payload.get("action")
            if action not in {"forward", "reply"}:
                return _result(DeliveryStatus.FAILED, "unsupported_email_action")
            note = str(payload.get("draft_text") or "").strip()
            original = None
            if self.store is not None and payload.get("source_id"):
                original = self.store.get_mail(effect.profile_id, str(payload["source_id"]))
            if action == "reply":
                if not note:
                    return _result(DeliveryStatus.FAILED, "empty_body")
                subject = "Re: " + _one_line(original.subject) if original else "Ответ"
                body = note
            else:
                if original is None:
                    return _result(DeliveryStatus.FAILED, "source_not_found")
                subject = "Fwd: " + _one_line(original.subject)
                parts = []
                if self._was_approved(effect):
                    parts.append("Пересылка согласована руководителем в Telegram.")
                if note:
                    parts.append(note)
                parts.append(
                    f"---------- Пересылаемое письмо ----------\nОт: {_one_line(original.sender, 320)}\n"
                    f"Тема: {_one_line(original.subject)}\n\n{original.text}"
                )
                body = "\n\n".join(parts)
        message = EmailMessage()
        message["From"] = self.settings.username
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = f"<{effect.effect_id}@{self.settings.username.split('@')[1]}>"
        message.set_content(body)
        return message

    # -- delivery -----------------------------------------------------------
    def execute(self, effect: Effect) -> EffectResult:
        return self.send_email(effect)

    def send_email(self, effect: Effect) -> EffectResult:
        if effect.kind != EffectKind.EMAIL:
            return _result(DeliveryStatus.FAILED, "unsupported_effect_kind")
        recipient = self._recipient(effect)
        if recipient is None:
            return _result(DeliveryStatus.FAILED, "unknown_recipient")
        pin = effect.payload.get("recipient_fingerprint")
        if pin is not None and pin != recipient_fingerprint(recipient):
            return _result(DeliveryStatus.FAILED, "recipient_changed")
        if pin is None and self.require_recipient_fingerprint:
            return _result(DeliveryStatus.FAILED, "recipient_not_pinned")
        message = self.build_message(effect, recipient)
        if isinstance(message, EffectResult):
            return message
        return self._deliver(message)

    def send_message(self, message: EmailMessage) -> EffectResult:
        """Send a prepared message (used by the CLI connectivity check)."""
        recipient = valid_address(str(message["To"]))
        if recipient is None or message["From"] != self.settings.username:
            return _result(DeliveryStatus.FAILED, "invalid_test_message")
        return self._deliver(message)

    def _deliver(self, message: EmailMessage) -> EffectResult:
        connection = None
        phase = "connect"
        try:
            connection = self.smtp_factory(
                self.settings.host, self.settings.port,
                timeout=self.settings.timeout_seconds, context=ssl.create_default_context(),
            )
            connection.login(self.settings.username, self.settings.password)
            phase = "send"
            connection.send_message(message, from_addr=self.settings.username, to_addrs=[message["To"]])
            return _result(DeliveryStatus.SENT, external_id=str(message["Message-ID"]).strip("<>")[:128])
        except Exception as error:  # noqa: BLE001 - every failure is mapped to a queue status
            if phase == "send" and getattr(connection, "data_started", None) is False:
                phase = "envelope"
            return self._classify(error, phase)
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except Exception:  # noqa: BLE001 - the outcome is already decided
                    pass

    @staticmethod
    def _classify(error: Exception, phase: str) -> EffectResult:
        def by_code(code: int, name: str) -> EffectResult:
            status = DeliveryStatus.RETRYABLE if 400 <= code < 500 else DeliveryStatus.FAILED
            return _result(status, f"smtp_{name}_{code}")

        if isinstance(error, smtplib.SMTPRecipientsRefused):
            codes = [code for code, _ in error.recipients.values()]
            return _result(
                DeliveryStatus.RETRYABLE if any(400 <= c < 500 for c in codes) else DeliveryStatus.FAILED,
                "smtp_recipient_refused",
            )
        if isinstance(error, smtplib.SMTPAuthenticationError):
            return by_code(error.smtp_code, "auth")
        if isinstance(error, smtplib.SMTPSenderRefused):
            return by_code(error.smtp_code, "sender_refused")
        if isinstance(error, smtplib.SMTPResponseException):
            # Server answered explicitly: the message was not accepted.
            return by_code(error.smtp_code, "response")
        if isinstance(error, smtplib.SMTPNotSupportedError):
            return _result(DeliveryStatus.FAILED, "smtp_not_supported")
        if phase == "envelope":
            return _result(DeliveryStatus.RETRYABLE, "smtp_envelope_failed")
        if phase == "connect":
            return _result(DeliveryStatus.RETRYABLE, "smtp_connect_failed")
        # Disconnect or timeout once sending began: the server may have accepted the message.
        return _result(DeliveryStatus.UNKNOWN, "smtp_response_lost")


def send_email(effect: Effect, *, settings: SmtpSettings | None = None, store: Store | None = None) -> EffectResult:
    """Public entry point from the architecture contract: deliver one email effect.

    Usable directly as an `EffectTransport.execute` callable wrapper; settings come from the
    environment unless given. Missing configuration is a permanent, safe `failed` (nothing was sent).
    """
    try:
        active = settings if settings is not None else SmtpSettings.from_env()
    except ValueError:
        return _result(DeliveryStatus.FAILED, "smtp_invalid_settings")
    if active is None:
        return _result(DeliveryStatus.FAILED, "smtp_not_configured")
    return SmtpEmailTransport(active, store=store).send_email(effect)
