from contextlib import closing
from json import loads
from sqlite3 import connect

import httpx

from app.contracts import Decision, EffectKind, TelegramActor
from app.core.approvals import ApprovalService, EffectIntent
from app.core.effects import EffectQueue
from app.core.store import Store
from app.telegram.__main__ import DemoRevision, DemoTransport, seed_approval
from app.telegram.bot import ApprovalNoticeTransport, TelegramAPI, handle_update


def test_callback_checks_actor_and_version_and_queues_one_effect(tmp_path):
    calls = []

    def respond(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    store = Store(tmp_path / "demo.sqlite3")
    store.initialize()
    _, card = seed_approval(store, 10001)
    approvals = ApprovalService(store)
    queue = EffectQueue(store)
    leader = TelegramActor(telegram_user_id=10001)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramAPI("test-token", client)
        transport = DemoTransport(ApprovalNoticeTransport(api, approvals, lambda *_: -123))
        assert queue.run_once(transport) is not None
        method, payload = calls[-1]
        assert method == "sendMessage"
        assert payload["chat_id"] == -123
        assert "demo@example.invalid" in payload["text"]
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        assert [button["text"] for button in buttons] == ["Утвердить", "Отклонить", "На правку"]

        def press(user_id, approval_id):
            handle_update(api, approvals, {"callback_query": {
                "id": "demo-callback",
                "from": {"id": user_id},
                "data": f"a:{approval_id}",
                "message": {"message_id": 42, "chat": {"id": -123}},
            }})

        press(10002, card.request.approval_id)
        assert calls[-1][1]["show_alert"] is True
        assert "нет доступа" in calls[-1][1]["text"]
        assert approvals.get_pending("telegram-demo", card.request.approval_id, leader) == card

        document = card.document
        revised = approvals.revise_document(
            document.profile_id, document.document_id, leader, document.version, document.candidate,
            mail_dispatch=document.mail_dispatch,
            intents=[EffectIntent(kind=EffectKind.EMAIL, target_ref="demo@example.invalid")],
        )
        press(10001, card.request.approval_id)
        assert "устарела" in calls[-1][1]["text"]
        assert queue.run_once(transport) is not None
        assert queue.run_once(transport) is None
        press(10001, revised.request.approval_id)
        assert [method for method, _ in calls[-3:]] == [
            "answerCallbackQuery", "editMessageReplyMarkup", "sendMessage",
        ]
        saved = approvals.get_decision("telegram-demo", revised.request.approval_id, leader)
        assert saved.decision == Decision.APPROVE
        assert saved.version == 2
        press(10001, revised.request.approval_id)
        assert "уже сохранено" in calls[-1][1]["text"]
        assert queue.run_once(transport) is not None
        assert queue.run_once(transport) is None


def test_revision_in_private_chat_opens_new_version_and_survives_restart(tmp_path):
    store = Store(tmp_path / "demo.sqlite3")
    store.initialize()
    document, card = seed_approval(store, 10001)
    approvals = ApprovalService(store)

    def respond(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    db = connect(tmp_path / "inbox.sqlite3")
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            api = TelegramAPI("test-token", client)
            session = DemoRevision(db, approvals, document.document_id)
            handle_update(api, approvals, {"callback_query": {
                "id": "revision",
                "from": {"id": 10001},
                "data": f"v:{card.request.approval_id}",
            }}, on_revision=session.open)
            for user_id in [10002, 10001]:
                assert session.handle(api, {"message": {
                    "from": {"id": user_id},
                    "chat": {"id": user_id, "type": "private"},
                    "text": "/revise Обновлённое письмо.",
                }})
                saved = store.get_version(document.profile_id, document.document_id, 2)
                if user_id == 10002:
                    assert saved is None
                else:
                    assert saved.candidate.draft_text == "Обновлённое письмо."
            assert approvals.list_preferences(document.profile_id) == ()
    finally:
        db.close()
    _, pending = seed_approval(store, 10001)
    assert pending.document.version == 2


def test_remember_is_explicit_private_and_deduplicated(tmp_path):
    store = Store(tmp_path / "demo.sqlite3")
    store.initialize()
    document, card = seed_approval(store, 10001)
    approvals = ApprovalService(store)
    sent = []

    def respond(request):
        sent.append(loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with (
        closing(connect(tmp_path / "inbox.sqlite3")) as db,
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
    ):
        api = TelegramAPI("test-token", client)
        handler = DemoRevision(db, approvals, document.document_id)

        def send(user_id, text, chat_type="private"):
            return handler.handle(api, {"message": {
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": chat_type},
                "text": text,
            }})

        assert not send(10001, "/remember Запись из группы", chat_type="group")
        assert not sent
        assert send(10002, "/remember Чужое правило")
        assert "только руководитель" in sent[-1]["text"]
        assert send(10001, "/remember")
        assert "от 1 до 500" in sent[-1]["text"]
        assert approvals.list_preferences(document.profile_id) == ()
        assert send(10001, "/remember Сначала указывай срок поставки")
        assert send(10001, "/remember Сначала указывай срок поставки")
        assert "Правило сохранено" in sent[-1]["text"]
        assert approvals.list_preferences(document.profile_id) == ("Сначала указывай срок поставки",)
        assert approvals.get_pending(
            document.profile_id, card.request.approval_id, TelegramActor(telegram_user_id=10001),
        ) == card

    assert ApprovalService(Store(store.path)).list_preferences(document.profile_id) == (
        "Сначала указывай срок поставки",
    )


def test_minutes_edits_validate_fields_and_approval_uses_latest_version(tmp_path):
    store = Store(tmp_path / "demo.sqlite3")
    store.initialize()
    document, card = seed_approval(store, 10001, "minutes")
    approvals = ApprovalService(store)
    actor = TelegramActor(telegram_user_id=10001)
    calls = []

    def respond(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with (
        closing(connect(tmp_path / "inbox.sqlite3")) as db,
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
    ):
        api = TelegramAPI("test-token", client)
        session = DemoRevision(db, approvals, document.document_id)
        queue = EffectQueue(store)
        effects = []
        demo = DemoTransport(ApprovalNoticeTransport(api, approvals, lambda *_: 10001))

        class Transport:
            def execute(self, effect):
                if effect.kind != EffectKind.TELEGRAM:
                    effects.append(effect)
                return demo.execute(effect)

        transport = Transport()

        def edit(value):
            return session.handle(api, {"message": {
                "from": {"id": 10001},
                "chat": {"id": 10001, "type": "private"},
                "text": f"/revise {value}",
            }})

        for step, value in enumerate(
            ("assignee demo-maria", "due 2026-12-08", "summary Обновлённый итог.", "recipient demo-manager"),
        ):
            assert queue.run_once(transport) is not None
            assert calls[-1][0] == "sendMessage"
            handle_update(api, approvals, {"callback_query": {
                "id": f"revision-{step}",
                "from": {"id": 10001},
                "data": f"v:{card.request.approval_id}",
                "message": {"message_id": 42, "chat": {"id": 10001}},
            }}, on_revision=session.open)
            assert len(calls[-3][1]["text"]) <= 200
            if step == 0:
                assert edit("assignee unknown")
                assert store.get_version(document.profile_id, document.document_id, 2) is None
            assert edit(value)
            _, card = seed_approval(store, 10001, "minutes")
            assert card.document.version == step + 2
        assert queue.run_once(transport) is not None
        assert "Адресаты: demo-manager" in calls[-1][1]["text"]
        assert "demo-maria, 2026-12-08" in calls[-1][1]["text"]
        assert queue.run_once(transport) is None
        assert not effects
        approvals.decide(card.request.approval_id, actor, Decision.APPROVE)
        while queue.run_once(transport) is not None:
            pass
        assert {(effect.kind, effect.target_ref, effect.version) for effect in effects} == {
            (EffectKind.EMAIL, "demo-manager", 5),
            (EffectKind.TASK, "demo-delivery", 5),
        }
