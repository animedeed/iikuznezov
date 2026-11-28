"""Mail settings. Read from the process environment; kept here so shared config files stay untouched."""

from dataclasses import dataclass, field
from json import JSONDecodeError, loads
from os import environ


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
        if "@" not in self.username or not self.password:
            raise ValueError("Set YANDEX_MAIL_ADDRESS (full address) and YANDEX_MAIL_APP_PASSWORD together")
        if self.timeout_seconds <= 0:
            raise ValueError("SMTP timeout must be positive")

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
