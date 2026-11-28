"""Mail settings. Read from the process environment; kept here so shared config files stay untouched."""

import math
import re
from dataclasses import dataclass, field
from email.errors import HeaderParseError
from email.headerregistry import Address
from json import JSONDecodeError, loads
from os import environ


def valid_address(value: str) -> str | None:
    """Accept precisely one ordinary ASCII mailbox address."""
    if not isinstance(value, str) or not value.isascii():
        return None
    address = value.strip(" ")
    if not address or any(ord(char) <= 32 or ord(char) == 127 for char in address):
        return None
    try:
        parsed = Address(addr_spec=address)
    except (HeaderParseError, ValueError, IndexError):
        return None
    labels = parsed.domain.split(".")
    if (
        parsed.addr_spec != address or not parsed.username or len(parsed.username) > 64
        or len(address) > 254 or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.I) for label in labels)
    ):
        return None
    return address


@dataclass(frozen=True)
class SmtpSettings:
    """Outgoing mail settings. The sender address doubles as the only allowed From."""

    username: str
    password: str = field(repr=False)
    host: str = "smtp.yandex.ru"
    port: int = 465
    timeout_seconds: float = 20.0
    recipients: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if valid_address(self.username) != self.username or not self.password:
            raise ValueError("Set YANDEX_MAIL_ADDRESS (full address) and YANDEX_MAIL_APP_PASSWORD together")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("SMTP timeout must be finite and positive")
        if not self.host.strip() or not 0 < self.port <= 65535:
            raise ValueError("Set a valid SMTP host and port")

    @classmethod
    def from_env(cls) -> "SmtpSettings | None":
        username = environ.get("YANDEX_MAIL_ADDRESS", "").strip()
        password = environ.get("YANDEX_MAIL_APP_PASSWORD", "").strip()
        if not username and not password:
            return None
        raw = environ.get("MAIL_RECIPIENTS", "").strip() or "{}"
        try:
            recipients = loads(raw)
        except JSONDecodeError as error:
            raise ValueError("MAIL_RECIPIENTS must be a JSON object of ref -> address") from error
        if not isinstance(recipients, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in recipients.items()
        ):
            raise ValueError("MAIL_RECIPIENTS must be a JSON object of ref -> address")
        return cls(
            username=username, password=password,
            host=environ.get("SMTP_HOST", "").strip() or "smtp.yandex.ru",
            port=int(environ.get("SMTP_PORT", "").strip() or 465),
            timeout_seconds=float(environ.get("SMTP_TIMEOUT_SECONDS", "").strip() or 20),
            recipients=recipients,
        )
