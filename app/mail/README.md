# Отправка почты (SMTP)

`app.mail.smtp.send_email(effect)` и `SmtpEmailTransport` реализуют `EffectTransport` для email-эффектов.
Исходы: `sent`, `retryable` (явный временный отказ до приёма тела), `failed` (отказ по адресу/авторизации/настройкам),
`unknown` (обрыв после начала передачи — не повторять без сверки).

Переменные окружения: `YANDEX_MAIL_ADDRESS`, `YANDEX_MAIL_APP_PASSWORD`, `MAIL_RECIPIENTS` (JSON `{"ссылка": "адрес"}`),
необязательно `SMTP_HOST` (smtp.yandex.ru), `SMTP_PORT` (465), `SMTP_TIMEOUT_SECONDS` (20).

Проверка доступа: `python -m app.mail --to you@example.com`.
Подключение к очереди выполняет владелец основы: `queue.run_once(SmtpEmailTransport(settings, store=store))`.
