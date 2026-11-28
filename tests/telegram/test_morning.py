from datetime import UTC, datetime
from json import loads

import httpx

from app.core.store import Store
from app.telegram.__main__ import seed_profile
from app.telegram.bot import TelegramAPI
from app.telegram.demo_plans import build_demo_plan
from app.telegram.morning import MorningPlans


def test_morning_plan_uses_local_time_and_survives_restart(tmp_path):
    posts = []

    def telegram(request):
        posts.append(loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    path = tmp_path / "schedule.sqlite3"
    store = Store(path)
    store.initialize()
    seed_profile(store, 10)

    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        api = TelegramAPI("test-token", client)
        morning = MorningPlans(store, api, "telegram-demo", 10, "Europe/Moscow", build_demo_plan)
        morning.tick(datetime(2026, 12, 7, 5, 29, tzinfo=UTC))
        morning.tick(datetime(2026, 12, 7, 5, 31, tzinfo=UTC))
        assert not posts
        morning.tick(datetime(2026, 12, 8, 5, 30, tzinfo=UTC))
        assert posts[0]["chat_id"] == 10
        assert "План на день · 08.12.2026" in posts[0]["text"]

        reopened = Store(path)
        reopened.initialize()
        restarted = MorningPlans(reopened, api, "telegram-demo", 10, "Europe/Moscow", build_demo_plan)
        restarted.tick(datetime(2026, 12, 8, 5, 30, 45, tzinfo=UTC))
        assert len(posts) == 1
        restarted.tick(datetime(2026, 12, 9, 5, 30, tzinfo=UTC))
        assert len(posts) == 2
        assert "План на день · 09.12.2026" in posts[-1]["text"]


def test_rate_limit_persists_retry_and_expired_plan_is_not_sent(tmp_path):
    posts = []

    def telegram(request):
        posts.append(request)
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 30}})

    store = Store(tmp_path / "schedule.sqlite3")
    store.initialize()
    seed_profile(store, 10)
    when = datetime(2026, 12, 7, tzinfo=UTC).date()

    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        morning = MorningPlans(store, TelegramAPI("test-token", client), "telegram-demo", 10,
                               "Europe/Moscow", build_demo_plan)
        effect_id = morning.enqueue(when)
        morning.drain(when)
        assert morning.queue.status(effect_id).status == "retryable"
        assert len(posts) == 1
        with store._connection(write=True) as db:
            db.execute("UPDATE effects SET next_attempt_at = 0 WHERE effect_id = ?", (effect_id,))
        morning.drain(datetime(2026, 12, 8, tzinfo=UTC).date())
        assert len(posts) == 1
        assert morning.queue.status(effect_id).error_code == "plan_expired_or_unbound"
