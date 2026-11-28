"""Run the m001 fixture through actual Telegram and SMTP adapters."""

from argparse import ArgumentParser
from dataclasses import replace
from hashlib import sha256
from json import dumps, loads
from os import environ
from pathlib import Path
from time import sleep

import httpx

from app.contracts import (
    DeliveryStatus, Effect, EffectKind, EffectResult, MailCandidate, MailDispatch,
    MailReceipt, PolicyClass, Profile, Route, TelegramActor,
)
from app.core.approvals import ApprovalService, EffectIntent
from app.core.effects import EffectQueue
from app.core.store import Store
from app.mail.config import SmtpSettings
from app.mail.smtp import SmtpEmailTransport, SmtpFactory, _valid_address
from app.telegram.bot import TelegramAPI, TelegramError
from app.telegram.channel import TelegramChannel
from app.telegram.demo_plans import build_demo_plan

PROFILE = "integration-m001"
_FIXTURE_PATH = Path("docs/CONTRACT_EXAMPLES.json")


class M001Run:
    def __init__(
        self, store: Store, api: TelegramAPI, *, bot_id: int, bot_username: str,
        user_id: int, chat_id: int, recipient: str, smtp_settings: SmtpSettings,
        fixture_path: Path = _FIXTURE_PATH, smtp_factory: SmtpFactory | None = None,
    ):
        address = _valid_address(recipient)
        if address is None:
            raise ValueError("Configure exactly one valid test recipient")
        if user_id <= 0 or chat_id == 0 or bot_id <= 0:
            raise ValueError("Invalid test bot, approver or chat")
        self.store = store
        self.api = api
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.actor = TelegramActor(telegram_user_id=user_id)
        self.recipient_ref = "m001-test-" + sha256(address.encode()).hexdigest()
        sender = _valid_address(smtp_settings.username)
        if sender is None:
            raise ValueError("Configure a valid SMTP sender")
        self._bind_run(sha256(sender.encode()).hexdigest())
        self.approvals = ApprovalService(store)
        self.queue = EffectQueue(store)
        self.telegram = TelegramChannel(
            self.approvals, api, bot_id=bot_id, bot_username=bot_username, context_db=store.path,
            chat_for=self._chat_for, profiles_for=lambda _: {}, build_plan=build_demo_plan,
        )
        settings = replace(smtp_settings, recipients={self.recipient_ref: address})
        smtp_options = {"smtp_factory": smtp_factory} if smtp_factory is not None else {}
        self.smtp = SmtpEmailTransport(settings, store=store, **smtp_options)
        self.document_id = self._seed_card(fixture_path)

    def _bind_run(self, sender_ref: str) -> None:
        self.store.initialize()
        binding = (self.bot_id, self.actor.telegram_user_id, self.chat_id, self.recipient_ref, sender_ref)
        with self.store._connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS integration_m001 (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bot_id INTEGER NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                recipient_ref TEXT NOT NULL, sender_ref TEXT NOT NULL
            )""")
            record = db.execute(
                "SELECT bot_id, user_id, chat_id, recipient_ref, sender_ref FROM integration_m001 WHERE id = 1"
            ).fetchone()
            if record is not None and tuple(record) != binding:
                raise ValueError("Test binding changed; use a separate integration database")
            if db.execute("SELECT 1 FROM profiles WHERE profile_id <> ?", (PROFILE,)).fetchone():
                raise ValueError("Use a dedicated m001 database")
            if record is None:
                if db.execute("SELECT 1 FROM profiles").fetchone():
                    raise ValueError("Use a fresh m001 database")
                db.execute("INSERT INTO integration_m001 VALUES (1, ?, ?, ?, ?, ?)", binding)
            db.execute("""CREATE TABLE IF NOT EXISTS integration_updates (
                bot_id INTEGER NOT NULL, update_id INTEGER NOT NULL, body TEXT,
                state TEXT NOT NULL CHECK (state IN ('pending', 'done')),
                PRIMARY KEY (bot_id, update_id)
            )""")

    def _chat_for(self, profile_id: str, actor: TelegramActor) -> int | None:
        return self.chat_id if profile_id == PROFILE and actor == self.actor else None

    def _seed_card(self, fixture_path: Path) -> str:
        fixtures = loads(fixture_path.read_text(encoding="utf-8"))
        fixture = next((row for row in fixtures["mail_receipts"] if row.get("fixture_id") == "m001"), None)
        if fixture is None:
            raise ValueError("m001 is missing from the contract fixtures")
        payload = {key: value for key, value in fixture.items() if key not in {"fixture_id", "channel_id"}}
        channel_id = "integration-m001-mail"
        self.store.seed_profiles((
            (Profile(profile_id=PROFILE, timezone="Europe/Moscow", quote_route=Route.REVIEW),
             channel_id, self.actor.telegram_user_id),
        ))
        receipt = self.store.receive_mail(channel_id, MailReceipt.model_validate(payload))
        candidate = MailCandidate(
            topic="Срок поставки (m001, учебный сценарий)",
            requires_review=True, policy_class=PolicyClass.CUSTOMER_COMMITMENT,
            rationale="Подтверждение обязательства перед клиентом требует решения руководителя.",
            draft_text="Учебное письмо m001. Срок поставки требует отдельного подтверждения.",
        )
        dispatch = MailDispatch(action="reply", target_ref=self.recipient_ref)
        document = self.store.record_draft(PROFILE, receipt.event_id, candidate, mail_dispatch=dispatch)
        try:
            self.approvals.propose(
                PROFILE, receipt.event_id, candidate, mail_dispatch=dispatch,
                intents=(EffectIntent(kind=EffectKind.EMAIL, target_ref=self.recipient_ref),),
            )
        except ValueError as error:
            if str(error) != "Current approval is closed":
                raise
        return document.document_id

    def execute(self, effect: Effect) -> EffectResult:
        if effect.profile_id == PROFILE and effect.document_id == self.document_id:
            if effect.kind == EffectKind.TELEGRAM:
                return self.telegram.execute(effect)
            if effect.kind == EffectKind.EMAIL and effect.target_ref == self.recipient_ref:
                return self.smtp.execute(effect)
        return EffectResult(status=DeliveryStatus.FAILED, error_code="unsupported_integration_effect")

    def flush(self) -> None:
        while self.queue.run_once(self) is not None:
            pass
        if self.telegram.delivery_updates.enqueue_latest(PROFILE, self.document_id):
            while self.queue.run_once(self) is not None:
                pass

    def _record_updates(self, updates: list[dict]) -> None:
        rows = []
        for update in updates:
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                continue
            callback = update.get("callback_query")
            message = callback.get("message") if isinstance(callback, dict) else None
            chat = message.get("chat") if isinstance(message, dict) else None
            relevant = isinstance(chat, dict) and chat.get("id") == self.chat_id
            rows.append((
                self.bot_id, update["update_id"], dumps(update) if relevant else None,
                "pending" if relevant else "done",
            ))
        with self.store._connection(write=True) as db:
            db.executemany(
                """INSERT INTO integration_updates (bot_id, update_id, body, state) VALUES (?, ?, ?, ?)
                ON CONFLICT (bot_id, update_id) DO NOTHING""", rows,
            )

    def _handle_pending(self) -> None:
        with self.store._connection() as db:
            pending = db.execute(
                """SELECT update_id, body FROM integration_updates
                WHERE bot_id = ? AND state = 'pending' ORDER BY update_id""", (self.bot_id,),
            ).fetchall()
        for update_id, body in pending:
            try:
                self.telegram.handle_update(loads(body))
            except TelegramError as error:
                # Telegram may reject a callback that expired between the decision and replay.
                if error.code != 400:
                    raise
            with self.store._connection(write=True) as db:
                db.execute(
                    """UPDATE integration_updates SET state = 'done', body = NULL
                    WHERE bot_id = ? AND update_id = ?""", (self.bot_id, update_id),
                )

    def poll_once(self) -> None:
        self._handle_pending()
        self.flush()
        with self.store._connection() as db:
            cursor = db.execute(
                "SELECT MAX(update_id) FROM integration_updates WHERE bot_id = ?", (self.bot_id,),
            ).fetchone()[0]
        self._record_updates(self.api.updates(cursor + 1 if cursor is not None else None))
        self._handle_pending()
        self.flush()


def check_test_chat(api: TelegramAPI, *, bot_id: int, user_id: int, chat_id: int) -> None:
    chat = api.call("getChat", chat_id=chat_id)
    if chat["type"] == "private":
        if chat_id != user_id:
            raise ValueError("A private test chat must belong to the configured approver")
        return
    if chat["type"] not in {"group", "supergroup"}:
        raise ValueError("Use a private test chat or test group")
    member = api.call("getChatMember", chat_id=chat_id, user_id=bot_id)
    status = member["status"]
    if status in {"creator", "administrator"}:
        return
    if status == "member" and (chat.get("permissions") or {}).get("can_send_messages", True):
        return
    if status == "restricted" and member.get("is_member") and member.get("can_send_messages"):
        return
    raise ValueError("The test bot must be allowed to send messages to the configured group")


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=int, help="Telegram ID of the test approver")
    parser.add_argument("--chat-id", required=True, type=int, help="Dedicated Telegram test chat")
    parser.add_argument("--database", type=Path, default=Path("data/integration-m001.sqlite3"))
    parser.add_argument("--fixtures", type=Path, default=_FIXTURE_PATH)
    args = parser.parse_args()
    token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    recipient = environ.get("IIKUZNEZOV_TEST_RECIPIENT", "").strip()
    if not token or _valid_address(recipient) is None:
        parser.error("Configure TELEGRAM_BOT_TOKEN and exactly one IIKUZNEZOV_TEST_RECIPIENT")
    if args.user_id <= 0 or args.chat_id == 0:
        parser.error("Use a positive user ID and a nonzero chat ID")
    try:
        settings = SmtpSettings.from_env()
    except ValueError as error:
        parser.error(str(error))
    if settings is None:
        parser.error("Configure YANDEX_MAIL_ADDRESS and YANDEX_MAIL_APP_PASSWORD")
    try:
        with httpx.Client(timeout=35) as client:
            api = TelegramAPI(token, client)
            bot = api.call("getMe")
            if api.call("getWebhookInfo")["url"]:
                parser.error("Use a dedicated test bot with no active webhook")
            check_test_chat(api, bot_id=bot["id"], user_id=args.user_id, chat_id=args.chat_id)
            run = M001Run(
                Store(args.database), api, bot_id=bot["id"], bot_username=bot["username"],
                user_id=args.user_id, chat_id=args.chat_id, recipient=recipient,
                smtp_settings=settings, fixture_path=args.fixtures,
            )
            print("m001: ожидание решения и доставки. Ctrl+C завершает запуск.", flush=True)
            while True:
                try:
                    run.poll_once()
                except TelegramError as error:
                    if error.code in (401, 403, 404, 409):
                        raise
                    sleep(max(2, error.retry_after))
    except (OSError, ValueError, TelegramError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
