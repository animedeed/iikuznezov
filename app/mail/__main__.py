"""Connectivity check: `python -m app.mail --to address@example.com` sends one short message."""

import argparse
import json
from email.message import EmailMessage

from app.mail.config import SmtpSettings
from app.mail.smtp import SmtpEmailTransport


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one test message over SMTP")
    parser.add_argument("--to", required=True)
    args = parser.parse_args()
    settings = SmtpSettings.from_env()
    if settings is None:
        parser.error("Set YANDEX_MAIL_ADDRESS and YANDEX_MAIL_APP_PASSWORD")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.username, args.to, "Проверка SMTP ИИкузницы"
    message.set_content("Тестовое сообщение. Если вы его видите, отправка работает.")
    outcome = SmtpEmailTransport(settings).send_message(message)
    print(json.dumps({"status": outcome.status.value, "error_code": outcome.error_code}, ensure_ascii=False))
    return 0 if outcome.status.value == "sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
