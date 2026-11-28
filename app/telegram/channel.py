"""Telegram entry points for the common workflow and effect dispatcher."""

from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date
from pathlib import Path
from sqlite3 import connect

from app.contracts import DeliveryStatus, Effect, EffectKind, EffectResult, Plan, TelegramActor
from app.core.approvals import ApprovalService
from app.telegram.bot import ApprovalNoticeTransport, TelegramAPI, handle_update
from app.telegram.delivery_updates import DeliveryUpdates
from app.telegram.plans import Period, PlanCommands


class TelegramChannel:
    def __init__(
        self, approvals: ApprovalService, api: TelegramAPI, *,
        bot_id: int, bot_username: str, context_db: Path,
        chat_for: Callable[[str, TelegramActor], int | None],
        profiles_for: Callable[[int], Mapping[str, str]],
        build_plan: Callable[[str, date, Period], Plan],
        week_ends_sunday: bool = False,
    ):
        self.approvals = approvals
        self.api = api
        self.bot_id = bot_id
        self.context_db = context_db
        self.chat_for = chat_for
        context_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(context_db)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS sent_cards (
                bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                approval_id TEXT NOT NULL, profile_id TEXT NOT NULL, approver_id INTEGER NOT NULL,
                PRIMARY KEY (bot_id, chat_id, message_id)
            )""")
        self.notices = ApprovalNoticeTransport(api, approvals, chat_for, self._record_card)
        self.delivery_updates = DeliveryUpdates(approvals, api, chat_for)
        self.plans = PlanCommands(
            api, profiles_for, build_plan, bot_username=bot_username, week_ends_sunday=week_ends_sunday,
        )

    def _record_card(
        self, approval_id: str, profile_id: str, approver: TelegramActor, chat_id: int, message_id: int,
    ) -> None:
        with closing(connect(self.context_db)) as db, db:
            db.execute(
                "INSERT INTO sent_cards VALUES (?, ?, ?, ?, ?, ?)",
                (self.bot_id, chat_id, message_id, approval_id, profile_id, approver.telegram_user_id),
            )

    def _check_card(self, approval_id: str, chat_id: int, message_id: int) -> bool:
        with closing(connect(self.context_db)) as db:
            binding = db.execute(
                """SELECT profile_id, approver_id FROM sent_cards
                   WHERE bot_id=? AND chat_id=? AND message_id=? AND approval_id=?""",
                (self.bot_id, chat_id, message_id, approval_id),
            ).fetchone()
        if binding is None:
            return False
        profile_id, approver_id = binding
        return self.chat_for(profile_id, TelegramActor(telegram_user_id=approver_id)) == chat_id

    def handle_update(
        self, update: dict, *, on_revision: Callable[[str, TelegramActor], str | None] | None = None,
    ) -> None:
        if not self.plans.handle(update):
            handle_update(
                self.api, self.approvals, update, on_revision=on_revision, check_card=self._check_card,
            )

    def execute(self, effect: Effect) -> EffectResult:
        if effect.kind == EffectKind.TELEGRAM:
            if effect.payload.get("operation") == "approval_notice":
                return self.notices.execute(effect)
            if effect.payload.get("operation") == "delivery_update":
                return self.delivery_updates.execute(effect)
        return EffectResult(status=DeliveryStatus.FAILED, error_code="unsupported_telegram_effect")
