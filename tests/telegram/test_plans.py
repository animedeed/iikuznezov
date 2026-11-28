from datetime import date
from json import loads

import httpx

from app.telegram.bot import TelegramAPI
from app.telegram.demo_plans import build_demo_plan
from app.telegram.plans import PlanCommands


def test_plan_commands_validate_actor_date_and_profile_before_loading():
    sent = []
    requests = []

    def telegram(request):
        sent.append(loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def load_plan(profile, when, period):
        requests.append((profile, when, period))
        return build_demo_plan(profile, when, period)

    profiles = {10: {"demo": "Демо"}, 20: {"a": "Первый", "b": "Второй"}}
    with httpx.Client(transport=httpx.MockTransport(telegram)) as client:
        commands = PlanCommands(
            TelegramAPI("test-token", client), lambda user: profiles.get(user, {}),
            load_plan, bot_username="examplebot",
        )

        def message(text, user=10, chat_type="private"):
            return commands.handle({"message": {
                "from": {"id": user},
                "chat": {"id": user, "type": chat_type},
                "text": text,
            }})

        assert message("/plan 2026-12-07", chat_type="group")
        assert not sent
        assert not message("/plan@otherbot 2026-12-07")
        assert message("/plan 2026-12-07", user=30)
        assert "нет доступа" in sent[-1]["text"]
        assert message("/plan 2026-02-30")
        assert "ГГГГ-ММ-ДД" in sent[-1]["text"]
        assert message("/plan 2026-12-07 b")
        assert "нет доступа" in sent[-1]["text"]
        assert message("/week 2026-12-07", user=20)
        assert "/week 2026-12-07 a" in sent[-1]["text"]
        assert "/week 2026-12-07 b" in sent[-1]["text"]
        assert not requests

        assert message("/plan 2026-12-07")
        assert "План на день · 07.12.2026" in sent[-1]["text"]
        assert "07.12 10:00 — Планёрка" in sent[-1]["text"]
        assert "Итоги рабочего дня 04.12" in sent[-1]["text"]
        assert message("/week@examplebot 2026-12-07 b", user=20)
        assert "План на неделю · 07.12.2026–13.12.2026" in sent[-1]["text"]
        assert "11.12 — Сообщить клиенту результат" in sent[-1]["text"]
        assert requests == [("demo", date(2026, 12, 7), "day"), ("b", date(2026, 12, 7), "week")]
