import json
from pathlib import Path

import httpx
import pytest

from app.core.store import Store
from app.integration import M001Run, PROFILE, check_test_chat
from app.mail.config import SmtpSettings
from app.telegram.bot import TelegramAPI


class Bot:
    def __init__(self):
        self.sent = []
        self.answers = []
        self.offsets = []
        self.updates = []

    def respond(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "sendMessage":
            self.sent.append(payload)
            result = {"message_id": len(self.sent)}
        elif method == "getUpdates":
            self.offsets.append(payload["offset"])
            result, self.updates = self.updates, []
        elif method == "answerCallbackQuery":
            self.answers.append(payload)
            result = True
        elif method == "editMessageReplyMarkup":
            result = True
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})


class SMTP:
    def __init__(self):
        self.sent = []

    def __call__(self, host, port, **options):
        assert host == "smtp.yandex.ru" and port == 465
        return self

    def login(self, user, password):
        assert user == "sender@example.invalid" and password == "fixture-password"

    def send_message(self, message, *, from_addr, to_addrs):
        self.sent.append((message, to_addrs))

    def quit(self):
        pass


def callback(update_id, data, *, actor=42, chat=-99, message=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}", "data": data, "from": {"id": actor},
            "message": {"message_id": message, "chat": {"id": chat, "type": "supergroup"}},
        },
    }


def open_run(
    database: Path, api: TelegramAPI, smtp: SMTP, *,
    recipient="receiver@example.invalid", sender="sender@example.invalid",
):
    return M001Run(
        Store(database), api, bot_id=7, bot_username="fixturebot",
        user_id=42, chat_id=-99, recipient=recipient,
        smtp_settings=SmtpSettings(username=sender, password="fixture-password"),
        smtp_factory=smtp,
    )


def test_m001_real_adapters_with_fake_providers_and_replay(tmp_path):
    bot = Bot()
    smtp = SMTP()
    database = tmp_path / "m001.sqlite3"
    with httpx.Client(transport=httpx.MockTransport(bot.respond)) as client:
        api = TelegramAPI("fixture-token", client)
        run = open_run(database, api, smtp)
        run.poll_once()
        assert len(bot.sent) == 1
        data = bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert "m001" in bot.sent[0]["text"]
        assert not smtp.sent

        bot.updates = [callback(1, data, actor=999), callback(2, data, chat=-100)]
        run.poll_once()
        assert len(bot.answers) == 1 and bot.answers[0]["show_alert"]
        assert not smtp.sent

        bot.updates = [callback(3, data), callback(4, data)]
        run.poll_once()
        assert len(smtp.sent) == 1
        message, recipients = smtp.sent[0]
        assert recipients == ["receiver@example.invalid"]
        assert "Учебное письмо m001" in message.get_content()
        assert str(message["Subject"]).startswith("Re: Подтверждение срока")
        assert "Утверждено" in bot.answers[1]["text"]
        assert "уже сохранено" in bot.answers[2]["text"]
        assert "Утверждено" in bot.sent[1]["text"]
        assert "Результат выполнения" in bot.sent[2]["text"] and "выполнено" in bot.sent[2]["text"]

        restarted = open_run(database, api, smtp)
        assert restarted.document_id == run.document_id
        bot.updates = [callback(4, data), callback(5, data)]
        restarted.poll_once()
        assert bot.offsets[-1] == 5
        assert len(smtp.sent) == 1
        assert len(bot.sent) == 3
        assert "уже сохранено" in bot.answers[-1]["text"]
        with restarted.store._connection() as db:
            effects = db.execute(
                "SELECT kind, status, attempts FROM effects WHERE profile_id=? AND kind='email'",
                (PROFILE,),
            ).fetchall()
            assert [tuple(row) for row in effects] == [("email", "sent", 1)]

        with pytest.raises(ValueError, match="binding changed"):
            open_run(database, api, smtp, recipient="different@example.invalid")
        with pytest.raises(ValueError, match="binding changed"):
            open_run(database, api, smtp, sender="different@example.invalid")


def test_durable_update_recovers_between_poll_and_callback(tmp_path):
    bot = Bot()
    smtp = SMTP()
    database = tmp_path / "m001.sqlite3"
    with httpx.Client(transport=httpx.MockTransport(bot.respond)) as client:
        api = TelegramAPI("fixture-token", client)
        run = open_run(database, api, smtp)
        run.flush()
        data = bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        run._record_updates([callback(10, data)])
        restarted = open_run(database, api, smtp)
        restarted.poll_once()
        assert bot.offsets == [11]
        assert len(smtp.sent) == 1
        assert "Результат выполнения" in bot.sent[-1]["text"]



def test_ambiguous_smtp_delivery_is_reported_and_never_retried_automatically(tmp_path):
    class UncertainSMTP(SMTP):
        def send_message(self, message, *, from_addr, to_addrs):
            super().send_message(message, from_addr=from_addr, to_addrs=to_addrs)
            raise TimeoutError("Provider may already have accepted the body")

    bot = Bot()
    smtp = UncertainSMTP()
    with httpx.Client(transport=httpx.MockTransport(bot.respond)) as client:
        api = TelegramAPI("fixture-token", client)
        database = tmp_path / "m001.sqlite3"
        run = open_run(database, api, smtp)
        run.poll_once()
        data = bot.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        bot.updates = [callback(1, data)]
        run.poll_once()
        assert "Утверждено" in bot.sent[1]["text"]
        assert "исход неизвестен" in bot.sent[2]["text"]

        bot.updates = [callback(2, data)]
        open_run(database, api, smtp).poll_once()
        assert len(smtp.sent) == 1 and len(bot.sent) == 3



@pytest.mark.parametrize("permission", [True, False])
def test_group_preflight_checks_bot_write_permission(permission):
    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "getChat":
            assert payload == {"chat_id": -99}
            result = {"type": "supergroup", "permissions": {"can_send_messages": permission}}
        else:
            assert method == "getChatMember"
            assert payload == {"chat_id": -99, "user_id": 7}
            result = {"status": "member"}
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramAPI("fixture-token", client)
        if permission:
            check_test_chat(api, bot_id=7, user_id=42, chat_id=-99)
        else:
            with pytest.raises(ValueError, match="allowed to send"):
                check_test_chat(api, bot_id=7, user_id=42, chat_id=-99)
