from datetime import date
from json import loads

import httpx
import pytest

from app.contracts import Decision, DeliveryStatus, EffectKind, EffectResult, TelegramActor
from app.core.approvals import ApprovalService
from app.core.effects import EffectQueue
from app.core.store import Store
from app.telegram.__main__ import seed_approval
from app.telegram.bot import TelegramAPI
from app.telegram.channel import TelegramChannel
from app.telegram.demo_plans import build_demo_plan


@pytest.mark.parametrize(("scenario", "button", "decision"), [
    ("mail", 0, Decision.APPROVE),
    ("minutes", 1, Decision.REJECT),
])
def test_common_dispatch_and_telegram_callback_context(tmp_path, scenario, button, decision):
    store = Store(tmp_path / "workflow.sqlite3")
    store.initialize()
    document, card = seed_approval(store, 10001, scenario)
    approvals = ApprovalService(store)
    queue = EffectQueue(store)
    actor = TelegramActor(telegram_user_id=10001)
    calls, business, plan_requests = [], [], []

    def respond(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    def load_plan(profile, when, period):
        plan_requests.append((profile, when, period))
        return build_demo_plan(profile, when, period)

    class Dispatcher:
        def execute(self, effect):
            if effect.kind == EffectKind.TELEGRAM:
                return channel.execute(effect)
            business.append(effect)
            return EffectResult(status=DeliveryStatus.SENT, external_id="workflow-test")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def make_channel():
            return TelegramChannel(
                approvals, TelegramAPI("test-token", client), bot_id=1, bot_username="examplebot",
                context_db=tmp_path / "telegram-context.sqlite3",
                chat_for=lambda profile, who: -100 if profile == document.profile_id and who == actor else None,
                profiles_for=lambda user: {document.profile_id: "Workflow"} if user == 10001 else {},
                build_plan=load_plan,
            )

        channel = make_channel()
        dispatcher = Dispatcher()
        assert queue.run_once(dispatcher) is not None
        assert not business
        data = calls[-1][1]["reply_markup"]["inline_keyboard"][0][button]["callback_data"]
        channel = make_channel()

        def press(user, chat, message=42):
            channel.handle_update({"callback_query": {
                "id": "query", "from": {"id": user}, "data": data,
                "message": {"chat": {"id": chat}, "message_id": message},
            }})

        press(10001, -200)
        assert calls[-1][1]["text"] == "Карточка недоступна."
        press(10001, -100, 43)
        assert calls[-1][1]["text"] == "Карточка недоступна."
        press(10002, -100)
        assert "нет доступа" in calls[-1][1]["text"]
        assert approvals.get_pending(document.profile_id, card.request.approval_id, actor)
        press(10001, -100)
        saved = approvals.get_decision(document.profile_id, card.request.approval_id, actor)
        assert saved.decision == decision
        press(10001, -100)
        assert "уже сохранено" in calls[-1][1]["text"]
        while queue.run_once(dispatcher) is not None:
            pass
        assert len(business) == (1 if decision == Decision.APPROVE else 0)

        channel.handle_update({"message": {
            "from": {"id": 10001}, "chat": {"id": 10001, "type": "private"}, "text": "/plan 2026-12-07",
        }})
        assert plan_requests == [(document.profile_id, date(2026, 12, 7), "day")]
