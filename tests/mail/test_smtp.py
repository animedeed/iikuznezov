import smtplib
import socket
from datetime import UTC, datetime

import pytest

from app.mail.config import SmtpSettings
from app.contracts import DeliveryStatus, Effect, MailReceipt
from app.core.effects import EffectQueue, enqueue_effect
from app.core.store import Store
from app.mail.smtp import SmtpEmailTransport, send_email

SETTINGS = SmtpSettings(
    username="bot@example.test", password="secret", recipients={"sales-desk": "sales@example.test", "bad": "a@b.c, d@e.f"},
)


class FakeSmtp:
    """Records calls; `fail` maps a step name to an exception to raise."""

    instances: list["FakeSmtp"] = []
    fail: dict[str, Exception] = {}

    def __init__(self, host, port, timeout, context):
        if "connect" in self.fail:
            raise self.fail["connect"]
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.quit_called = False
        FakeSmtp.instances.append(self)

    def login(self, user, password):
        if "login" in self.fail:
            raise self.fail["login"]
        self.credentials = (user, password)

    def send_message(self, message, from_addr, to_addrs):
        if "send" in self.fail:
            raise self.fail["send"]
        self.sent.append((message, from_addr, to_addrs))
        return {}

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSmtp.instances = []
    FakeSmtp.fail = {}


def effect(target="sales-desk", payload=None, effect_id="e1"):
    return Effect(
        effect_id=effect_id, profile_id="p1", key=f"k-{effect_id}", kind="email", target_ref=target,
        payload=payload if payload is not None else {"action": "reply", "source_id": "s1", "draft_text": "Отправляем КП."},
    )


def transport(store=None):
    return SmtpEmailTransport(SETTINGS, store=store, smtp_factory=FakeSmtp)


def test_reply_is_sent_to_resolved_recipient_with_stable_message_id():
    result = transport().send_email(effect())
    assert result.status == DeliveryStatus.SENT and result.external_id == "e1@example.test"
    message, from_addr, to_addrs = FakeSmtp.instances[0].sent[0]
    assert (from_addr, to_addrs) == ("bot@example.test", ["sales@example.test"])
    assert message["Message-ID"] == "<e1@example.test>"
    assert "Отправляем КП." in message.get_content()
    assert FakeSmtp.instances[0].credentials == ("bot@example.test", "secret")
    assert FakeSmtp.instances[0].quit_called and FakeSmtp.instances[0].port == 465


def test_forward_includes_original_mail(tmp_path):
    store = Store(tmp_path / "m.sqlite")
    store.initialize()
    with store._connection(write=True) as db:
        db.execute("INSERT INTO profiles VALUES ('p1', 'UTC', 'ignore')")
        db.execute("INSERT INTO bindings (kind, channel_id, profile_id) VALUES ('mail', 'box', 'p1')")
    receipt = MailReceipt.model_validate({
        "folder": "INBOX", "uidvalidity": 1, "uid": 5, "from": "buyer@example.invalid",
        "subject": "Запрос КП", "text": "Пришлите КП на десять единиц.", "received_at": datetime.now(UTC),
    })
    event_id = store.receive_mail("box", receipt).event_id
    payload = {"action": "forward", "source_id": event_id, "draft_text": None}
    result = transport(store).send_email(effect(payload=payload))
    assert result.status == DeliveryStatus.SENT
    message = FakeSmtp.instances[0].sent[0][0]
    assert message["Subject"] == "Fwd: Запрос КП"
    assert "Пришлите КП на десять единиц." in message.get_content()
    assert "Пересылка согласована" not in message.get_content()
    missing = transport(store).send_email(effect(payload={"action": "forward", "source_id": "nope"}, effect_id="e2"))
    assert (missing.status, missing.error_code) == (DeliveryStatus.FAILED, "source_not_found")


def test_minutes_payload_is_rendered():
    minutes = {"title": "Планёрка", "summary": "Обсудили сроки.", "decisions": [{"text": "Сдвинуть релиз"}],
               "open_questions": [], "tasks": [{"action": "Обновить план", "due_date": "2026-12-10"}]}
    transport().send_email(effect(payload={"minutes": minutes}))
    message = FakeSmtp.instances[0].sent[0][0]
    assert message["Subject"] == "Протокол: Планёрка"
    body = message.get_content()
    assert "Сдвинуть релиз" in body and "Обновить план (срок 2026-12-10)" in body


@pytest.mark.parametrize("target", ["missing-ref", "bad"])
def test_unknown_or_unsafe_recipient_fails_without_connecting(target):
    result = transport().send_email(effect(target=target))
    assert (result.status, result.error_code) == (DeliveryStatus.FAILED, "unknown_recipient")
    assert FakeSmtp.instances == []


def test_reply_without_text_and_other_kinds_fail():
    assert transport().send_email(effect(payload={"action": "reply", "draft_text": ""})).error_code == "empty_body"
    other = Effect(effect_id="t", profile_id="p1", key="t", kind="telegram", target_ref="x", payload={})
    assert transport().send_email(other).error_code == "unsupported_effect_kind"
    assert FakeSmtp.instances == []


@pytest.mark.parametrize(
    ("step", "error", "status", "code"),
    [
        ("connect", OSError("unreachable"), DeliveryStatus.RETRYABLE, "smtp_connect_failed"),
        ("connect", socket.timeout(), DeliveryStatus.RETRYABLE, "smtp_connect_failed"),
        ("login", smtplib.SMTPAuthenticationError(535, b"bad"), DeliveryStatus.FAILED, "smtp_auth_535"),
        ("login", smtplib.SMTPAuthenticationError(454, b"later"), DeliveryStatus.RETRYABLE, "smtp_auth_454"),
        ("send", smtplib.SMTPRecipientsRefused({"x": (550, b"no user")}), DeliveryStatus.FAILED, "smtp_recipient_refused"),
        ("send", smtplib.SMTPRecipientsRefused({"x": (451, b"try later")}), DeliveryStatus.RETRYABLE, "smtp_recipient_refused"),
        ("send", smtplib.SMTPSenderRefused(553, b"not yours", "bot@example.test"), DeliveryStatus.FAILED, "smtp_sender_refused_553"),
        ("send", smtplib.SMTPDataError(554, b"spam"), DeliveryStatus.FAILED, "smtp_response_554"),
        ("send", smtplib.SMTPDataError(451, b"busy"), DeliveryStatus.RETRYABLE, "smtp_response_451"),
        ("send", smtplib.SMTPServerDisconnected("lost"), DeliveryStatus.UNKNOWN, "smtp_response_lost"),
        ("send", socket.timeout(), DeliveryStatus.UNKNOWN, "smtp_response_lost"),
    ],
)
def test_error_mapping(step, error, status, code):
    FakeSmtp.fail = {step: error}
    result = transport().send_email(effect())
    assert (result.status, result.error_code) == (status, code)
    assert FakeSmtp.instances == [] or FakeSmtp.instances[0].quit_called


def test_queue_lost_response_is_not_resent(tmp_path):
    store = Store(tmp_path / "q.sqlite")
    store.initialize()
    with store._connection(write=True) as db:
        db.execute("INSERT INTO profiles VALUES ('p1', 'UTC', 'ignore')")
        enqueue_effect(db, effect())
    queue, smtp = EffectQueue(store), transport()
    FakeSmtp.fail = {"send": socket.timeout()}
    assert queue.run_once(smtp) == "e1"
    assert queue.status("e1").status == "unknown"
    assert queue.run_once(smtp) is None  # unknown is never retried automatically
    FakeSmtp.fail = {}
    queue.retry("e1", verified_by="operator")
    assert queue.run_once(smtp) == "e1"
    assert queue.status("e1").status == "sent"
    assert FakeSmtp.instances[-1].sent[0][0]["Message-ID"] == "<e1@example.test>"


def test_module_send_email_without_config_fails_safely(monkeypatch):
    monkeypatch.delenv("YANDEX_MAIL_ADDRESS", raising=False)
    monkeypatch.delenv("YANDEX_MAIL_APP_PASSWORD", raising=False)
    result = send_email(effect())
    assert (result.status, result.error_code) == (DeliveryStatus.FAILED, "smtp_not_configured")


def test_settings_from_env(monkeypatch):
    for name in ("YANDEX_MAIL_ADDRESS", "YANDEX_MAIL_APP_PASSWORD", "MAIL_RECIPIENTS"):
        monkeypatch.delenv(name, raising=False)
    assert SmtpSettings.from_env() is None
    monkeypatch.setenv("YANDEX_MAIL_ADDRESS", "bot@example.test")
    with pytest.raises(ValueError):
        SmtpSettings.from_env()
    monkeypatch.setenv("YANDEX_MAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_RECIPIENTS", '{"sales-desk": "s@example.test"}')
    loaded = SmtpSettings.from_env()
    assert loaded.host == "smtp.yandex.ru" and loaded.port == 465 and loaded.recipients == {"sales-desk": "s@example.test"}
    assert "pw" not in repr(loaded)


def test_pinned_recipient_cannot_drift():
    from app.mail.smtp import recipient_fingerprint

    smtp = SmtpEmailTransport(SETTINGS, smtp_factory=FakeSmtp, require_recipient_fingerprint=True)
    assert smtp.send_email(effect()).error_code == "recipient_not_pinned"
    candidate = effect()
    pinned_to_old_address = {**candidate.payload, "recipient_fingerprint": recipient_fingerprint("old@example.test")}
    result = smtp.send_email(candidate.model_copy(update={"payload": pinned_to_old_address}))
    assert (result.status, result.error_code) == (DeliveryStatus.FAILED, "recipient_changed")
    assert not FakeSmtp.instances

    pinned = {**candidate.payload, "recipient_fingerprint": recipient_fingerprint("sales@example.test")}
    assert smtp.send_email(candidate.model_copy(update={"payload": pinned})).status == DeliveryStatus.SENT


def test_timeout_before_data_is_safe_to_retry():
    class BeforeData(FakeSmtp):
        data_started = False

    BeforeData.fail = {"send": socket.timeout()}
    result = SmtpEmailTransport(SETTINGS, smtp_factory=BeforeData).send_email(effect())
    assert (result.status, result.error_code) == (DeliveryStatus.RETRYABLE, "smtp_envelope_failed")


@pytest.mark.parametrize("address", ["local@", "local@a..b", "local@bad_domain.test", "buyer@example.test\n"])
def test_invalid_from_is_rejected_during_configuration(address):
    with pytest.raises(ValueError, match="YANDEX_MAIL_ADDRESS"):
        SmtpSettings(username=address, password="secret")
