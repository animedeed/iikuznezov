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


def test_shared_pilot_runs_channels_and_scopes_knowledge(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.calendar.fake import FakeCalendar
    from app.contracts import MailReceipt, Profile, Route
    from app.core.approvals import ApprovalService
    from app.core.mail_polling import MailPolling
    from app.core.meeting_sessions import MeetingSessions, TurnRequest
    from app.core.pilot import PilotWorker
    from app.knowledge import GroundedAnswer, KnowledgeBase
    from app.mail.imap import ImapSettings, MailCursor, PollResult
    from app.mail.routing import MailRoutingContext
    from app.mail.smtp import SmtpEmailTransport, recipient_fingerprint
    from app.telegram.channel import TelegramChannel
    from app.telegram.inbox import UpdateInbox
    from app.testing import FakeModel
    from app.workflow.minutes import MinutesDraft
    from app.workflow.plan import build_plan, schedule_plan_message
    from app.workflow.tasks import SqliteTaskStore
    from app.workflow.transport import WorkflowTransport

    store = Store(tmp_path / "pilot.sqlite3")
    store.initialize()
    profile = Profile(profile_id="pilot", timezone="Europe/Moscow", quote_route=Route.REVIEW)
    other = profile.model_copy(update={"profile_id": "other"})
    store.seed_profiles(((profile, "mail-pilot", 42), (other, "mail-other", 99)))
    approvals = ApprovalService(store)
    now = datetime.now(UTC)
    receipt = MailReceipt.model_validate({
        "folder": "INBOX", "uidvalidity": 10, "uid": 1, "from": "sender@example.invalid",
        "subject": "Просьба подтвердить срок поставки", "text": "Нужно согласовать срок.",
        "received_at": now,
    })
    cursors = []

    def fake_poll(settings, persist, *, cursor):
        cursors.append(cursor)
        return PollResult(MailCursor(10, 1), (persist(settings.channel_id, receipt),))

    monkeypatch.setattr("app.core.mail_polling.poll_once", fake_poll)
    recipients = {"review": "review@example.invalid", "participant": "participant@example.invalid"}
    pins = {ref: recipient_fingerprint(address) for ref, address in recipients.items()}
    mail = MailPolling(
        store, ImapSettings("mail-pilot", "sender@example.invalid", "fixture-password"),
        MailRoutingContext(profile, targets={Route.REVIEW: "review"}),
        recipient_fingerprints=pins, model=None,
    )
    sessions = MeetingSessions(store, approvals)
    meeting_token = sessions.provision(
        "pilot", "pilot-meeting", 42, participant_refs=("participant",), allowed_assignee_refs=("sales",),
    )
    sessions.start("pilot-meeting", meeting_token, "start")
    sessions.turn("pilot-meeting", meeting_token, TurnRequest(request_id="turn", sequence=1, text="Подготовить отчёт."))
    sessions.finish("pilot-meeting", meeting_token, "finish")
    due_date = now.date() + timedelta(days=2)
    model = FakeModel(answer=MinutesDraft.model_validate({
        "summary": "Учебный итог.",
        "tasks": [{"action": "Подготовить отчёт", "assignee_ref": "sales", "due_date": due_date.isoformat(),
                   "source_turn_ids": ["turn"]}],
    }))

    bot, smtp = Bot(), SMTP()
    calendar, tasks = FakeCalendar(), SqliteTaskStore(store)
    isolated_plan = build_plan("other", now.date(), store=store, calendar=calendar, tasks=tasks)
    assert schedule_plan_message(store, isolated_plan)
    with httpx.Client(transport=httpx.MockTransport(bot.respond)) as client:
        api = TelegramAPI("fixture-token", client)
        channel = TelegramChannel(
            approvals, api, bot_id=7, bot_username="fixturebot", context_db=store.path,
            chat_for=lambda profile_id, actor: 42 if profile_id == "pilot" and actor.telegram_user_id == 42 else None,
            profiles_for=lambda user_id: {"pilot": "Pilot"} if user_id == 42 else {},
            build_plan=lambda profile_id, day, period: build_plan(
                profile_id, day, period, store=store, calendar=calendar, tasks=tasks,
            ),
        )
        inbox = UpdateInbox(store.path, api, 7)
        worker = PilotWorker(
            store, "pilot", channel, WorkflowTransport(calendar, tasks),
            smtp=SmtpEmailTransport(
                SmtpSettings(username="sender@example.invalid", password="fixture-password", recipients=recipients),
                store=store, smtp_factory=smtp, require_recipient_fingerprint=True,
            ),
            mail=mail, model=model, recipient_fingerprints=pins, bot_username="fixturebot",
        )
        worker.tick()
        cards = [(index, entry) for index, entry in enumerate(bot.sent, 1) if entry.get("reply_markup")]
        assert len(cards) == 2
        assert not smtp.sent and not calendar.written and not tasks.list_open("pilot")
        for update_id, (message_id, entry) in enumerate(cards, 1):
            data = entry["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            bot.updates.append(callback(update_id, data, chat=42, message=message_id))
        inbox.poll_once(worker.handle_update)
        inbox.poll_once(worker.handle_update)
        worker.tick()
        worker.tick()

        assert [cursor.uid for cursor in cursors if cursor] == [1, 1]
        assert sorted(x[1][0] for x in smtp.sent) == sorted(recipients.values())
        assert len(calendar.written) == 1 and len(tasks.list_open("pilot")) == 1
        assert not tasks.list_open("other")
        with store._connection() as db:
            assert db.execute("SELECT status FROM effects WHERE profile_id='other'").fetchone()[0] == "pending"

        source = KnowledgeBase(store)
        source.index("pilot", "known", "Четверг — дата отгрузки.")
        source.index("other", "private", "Среда — дата отгрузки.")
        model.answer = GroundedAnswer(answer="В четверг.", citations=[1])
        worker.handle_update({
            "message": {"text": "/ask дата отгрузки", "chat": {"type": "private", "id": 99}, "from": {"id": 99}},
        })
        assert len(model.calls) == 1
        worker.handle_update({
            "message": {"text": "/ask дата отгрузки", "chat": {"type": "private", "id": 42}, "from": {"id": 42}},
        })
        assert len(model.calls) == 2
        assert "known" in bot.sent[-1]["text"]
        assert "Среда" not in model.calls[-1][1][-1]["content"]
