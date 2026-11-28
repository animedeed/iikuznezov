from json import loads

import httpx

from app.contracts import Decision, DeliveryStatus, EffectKind, EffectResult, TelegramActor
from app.core.approvals import ApprovalService
from app.core.effects import EffectQueue
from app.core.store import Store
from app.telegram.__main__ import DemoTransport, seed_approval
from app.telegram.bot import ApprovalNoticeTransport, TelegramAPI
from app.telegram.delivery_updates import DeliveryUpdates
from app.telegram.demo_minutes import minutes_intents, revise_minutes


def test_result_notice_reports_partial_failure_and_final_status_once(tmp_path):
    path = tmp_path / "demo.sqlite3"
    store = Store(path)
    store.initialize()
    document, card = seed_approval(store, 10001, "minutes")
    approvals = ApprovalService(store)
    actor = TelegramActor(telegram_user_id=10001)
    revised = revise_minutes(revise_minutes(card.document.candidate, "assignee demo-maria"), "due 2026-12-08")
    card = approvals.revise_document(
        document.profile_id, document.document_id, actor, document.version, revised,
        intents=minutes_intents(revised),
    )
    sent = []

    def telegram(request):
        sent.append(loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    class RetryTaskOnce:
        first = True

        def execute(self, effect):
            if effect.kind == EffectKind.TASK and self.first:
                self.first = False
                return EffectResult(status=DeliveryStatus.RETRYABLE, error_code="demo_provider_busy")
            return transport.execute(effect)

    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        api = TelegramAPI("test-token", client)
        chat_for = lambda *_: 10001
        updates = DeliveryUpdates(approvals, api, chat_for)
        transport = DemoTransport(ApprovalNoticeTransport(api, approvals, chat_for), updates)
        retry = RetryTaskOnce()
        queue = EffectQueue(store)
        assert queue.run_once(retry) is not None
        assert not updates.enqueue_latest(document.profile_id, document.document_id)
        approvals.decide(card.request.approval_id, actor, Decision.APPROVE)
        while queue.run_once(retry) is not None:
            pass

        assert updates.enqueue_latest(document.profile_id, document.document_id)
        assert queue.run_once(retry) is not None
        assert "Отправка (demo-team) — выполнено" in sent[-1]["text"]
        assert "Создание поручения (demo-delivery) — ожидает повтора" in sent[-1]["text"]
        assert not updates.enqueue_latest(document.profile_id, document.document_id)
        with store._connection(write=True) as db:
            db.execute("UPDATE effects SET next_attempt_at = 0 WHERE kind = 'task'")
        assert queue.run_once(retry) is not None
        assert updates.enqueue_latest(document.profile_id, document.document_id)
        assert queue.run_once(retry) is not None
        assert "Создание поручения (demo-delivery) — выполнено" in sent[-1]["text"]

        reopened_updates = DeliveryUpdates(ApprovalService(Store(path)), api, chat_for)
        assert not reopened_updates.enqueue_latest(document.profile_id, document.document_id)
        assert len(sent) == 3
