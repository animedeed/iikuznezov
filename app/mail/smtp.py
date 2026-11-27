"""SMTP delivery for email effects. Implements `EffectTransport`; no I/O at import time."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from app.mail.config import SmtpSettings
from app.contracts import DeliveryStatus, Effect, EffectKind, EffectResult
from app.core.store import Store

SmtpFactory = Callable[..., smtplib.SMTP]


def _result(status: DeliveryStatus, code: str | None = None, external_id: str | None = None) -> EffectResult:
    return EffectResult(status=status, error_code=code, external_id=external_id)


def _valid_address(value: str) -> str | None:
    """Accept only a bare single address; reject lists, display names and control characters."""
    address = value.strip()
    if not address or any(ch in address for ch in "\r\n\0 ,;<>()\"") or address.count("@") != 1:
        return None
    local, domain = address.split("@")
    return address if local and "." in domain and not domain.endswith(".") else None


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
        smtp_factory: SmtpFactory = smtplib.SMTP_SSL,
    ):
        self.settings = settings
        self.store = store
        self.smtp_factory = smtp_factory

    # -- message building ---------------------------------------------------
    def _recipient(self, effect: Effect) -> str | None:
        address = self.settings.recipients.get(effect.target_ref)
        return _valid_address(address) if address else None

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
                body = (note + "\n\n" if note else "") + (
                    f"---------- Пересылаемое письмо ----------\nОт: {_one_line(original.sender, 320)}\n"
                    f"Тема: {_one_line(original.subject)}\n\n{original.text}"
                )
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
        message = self.build_message(effect, recipient)
        if isinstance(message, EffectResult):
            return message
        return self._deliver(message)

    def send_message(self, message: EmailMessage) -> EffectResult:
        """Send a prepared message (used by the CLI connectivity check)."""
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
        if phase == "connect":
            return _result(DeliveryStatus.RETRYABLE, "smtp_connect_failed")
        # Disconnect or timeout once sending began: the server may have accepted the message.
        return _result(DeliveryStatus.UNKNOWN, "smtp_response_lost")


def send_email(effect: Effect, *, settings: SmtpSettings | None = None, store: Store | None = None) -> EffectResult:
    """Public entry point from the architecture contract: deliver one email effect.

    Usable directly as an `EffectTransport.execute` callable wrapper; settings come from the
    environment unless given. Missing configuration is a permanent, safe `failed` (nothing was sent).
    """
    active = settings if settings is not None else SmtpSettings.from_env()
    if active is None:
        return _result(DeliveryStatus.FAILED, "smtp_not_configured")
    return SmtpEmailTransport(active, store=store).send_email(effect)
