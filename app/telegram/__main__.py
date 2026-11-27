"""Run a synthetic Telegram approval on a dedicated local database."""

from argparse import ArgumentParser
from contextlib import closing
from datetime import UTC, datetime
from json import dumps, loads
from os import environ
from pathlib import Path
from sqlite3 import Connection, connect
from time import sleep, time

import httpx
from pydantic import ValidationError

from app.contracts import (
    Decision, DeliveryStatus, EffectKind, EffectResult, MailCandidate, MailDispatch,
    MailReceipt, MinutesCandidate, Profile, Route, TelegramActor,
)
from app.core.approvals import ApprovalService, EffectIntent
from app.core.effects import EffectQueue
from app.core.store import Store
from app.telegram.bot import ApprovalNoticeTransport, TelegramAPI, TelegramError, handle_update
from app.telegram.delivery_updates import DeliveryUpdates
from app.telegram.demo_minutes import REVISION_HINT, minutes_intents, prepare_minutes, revise_minutes
from app.telegram.demo_plans import build_demo_plan
from app.telegram.morning import MorningPlans
from app.telegram.plans import PlanCommands


def seed_profile(store: Store, user_id: int):
    store.seed_profiles([
        (Profile(profile_id="telegram-demo", timezone="Europe/Moscow", quote_route=Route.REVIEW),
         "telegram-demo-mail", user_id),
    ])


def seed_approval(store: Store, user_id: int, scenario: str = "mail"):
    seed_profile(store, user_id)
    approvals = ApprovalService(store)
    if scenario == "minutes":
        source_id, candidate = prepare_minutes(approvals, user_id)
        dispatch = None
        intents = minutes_intents(candidate)
    else:
        source_id, candidate, dispatch = prepare_mail(store, user_id)
        intents = [EffectIntent(kind=EffectKind.EMAIL, target_ref=dispatch.target_ref)]
    document = store.record_draft("telegram-demo", source_id, candidate, mail_dispatch=dispatch)
    try:
        card = approvals.propose(
            "telegram-demo", source_id, candidate, mail_dispatch=dispatch, intents=intents,
        )
    except ValueError as error:
        if str(error) != "Current approval is closed":
            raise
        card = None
    return document, card


def prepare_mail(store: Store, user_id: int):
    receipt = MailReceipt.model_validate({
        "folder": "INBOX", "uid": user_id, "uidvalidity": 1,
        "from": "demo@example.invalid", "subject": "Срок поставки",
        "text": "Уточните срок поставки.", "received_at": "2026-12-07T07:00:00Z",
    })
    source = store.receive_mail("telegram-demo-mail", receipt)
    dispatch = MailDispatch(action="reply", target_ref="demo@example.invalid")
    candidate = MailCandidate(
        topic="terms", requires_review=True, rationale="Требуется подтверждение срока",
        draft_text="Добрый день! Сообщим срок поставки после уточнения.",
    )
    return source.event_id, candidate, dispatch


class DemoTransport:
    def __init__(self, notices: ApprovalNoticeTransport, updates: DeliveryUpdates | None = None):
        self.notices = notices
        self.updates = updates

    def execute(self, effect):
        if self.updates is not None and effect.kind == EffectKind.TELEGRAM:
            if effect.payload.get("operation") == "delivery_update":
                return self.updates.execute(effect)
        if effect.kind == EffectKind.EMAIL and effect.profile_id == "telegram-demo":
            if "minutes" in effect.payload:
                print(f"Пробная отправка протокола: {effect.target_ref}.", flush=True)
            else:
                print("Пробная отправка письма зарегистрирована.", flush=True)
            return EffectResult(status=DeliveryStatus.SENT, external_id="demo-email")
        if effect.kind == EffectKind.TASK and effect.profile_id == "telegram-demo":
            print(f"Пробное создание поручения: {effect.target_ref}.", flush=True)
            return EffectResult(status=DeliveryStatus.SENT, external_id="demo-task")
        return self.notices.execute(effect)


class DemoRevision:
    def __init__(self, db: Connection, approvals: ApprovalService, document_id: str):
        self.db = db
        self.approvals = approvals
        self.document_id = document_id
        db.execute("""CREATE TABLE IF NOT EXISTS revision
            (approval_id TEXT PRIMARY KEY, user_id INTEGER, version INTEGER, expires REAL)""")

    def open(self, approval_id, actor):
        decision = self.approvals.get_decision("telegram-demo", approval_id, actor)
        if decision is None or decision.decision != Decision.REVISE or decision.document_id != self.document_id:
            return None
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revision VALUES (?, ?, ?, ?)",
                            (approval_id, actor.telegram_user_id, decision.version, time() + 600))
        document = self.approvals.store.get_version("telegram-demo", self.document_id, decision.version)
        if document is not None and isinstance(document.candidate, MinutesCandidate):
            return f"Сессия правки открыта на 10 минут. {REVISION_HINT}"
        return "За 10 минут отправьте /revise и новый текст письма в личном чате с ботом."

    def handle(self, api: TelegramAPI, update: dict) -> bool:
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        text = message.get("text")
        chat = message.get("chat")
        sender = message.get("from")
        if (
            not isinstance(text, str)
            or not isinstance(chat, dict) or chat.get("type") != "private"
            or not isinstance(sender, dict) or type(sender.get("id")) is not int
            or sender["id"] <= 0 or chat.get("id") != sender["id"]
        ):
            return False
        command, _, entry = text.partition(" ")
        if command not in ("/revise", "/remember"):
            return False
        actor = TelegramActor(telegram_user_id=sender["id"])
        draft = entry.strip()
        if command == "/remember":
            self.remember(api, actor, draft)
            return True
        session = self.db.execute(
            "SELECT approval_id, version FROM revision WHERE user_id = ? AND expires > ? "
            "ORDER BY version DESC LIMIT 1",
            (actor.telegram_user_id, time()),
        ).fetchone()
        if session is None or not draft:
            api.call("sendMessage", chat_id=chat["id"], text="Нужна открытая сессия правки и новый текст.")
            return True
        approval_id, version = session
        document = self.approvals.store.get_version("telegram-demo", self.document_id, version)
        if document is None:
            return True
        try:
            if isinstance(document.candidate, MinutesCandidate):
                candidate = revise_minutes(document.candidate, draft)
                intents = minutes_intents(candidate)
            else:
                candidate = MailCandidate.model_validate({**document.candidate.model_dump(), "draft_text": draft})
                intents = [EffectIntent(kind=EffectKind.EMAIL, target_ref=document.mail_dispatch.target_ref)]
        except ValidationError:
            api.call("sendMessage", chat_id=chat["id"], text="Значение не прошло проверку. Уточните правку.")
            return True
        except ValueError as error:
            api.call("sendMessage", chat_id=chat["id"], text=str(error))
            return True
        try:
            self.approvals.revise_document(
                document.profile_id, document.document_id, actor, version, candidate,
                mail_dispatch=document.mail_dispatch,
                intents=intents,
            )
            status = "Правка сохранена. Бот пришлёт карточку новой версии."
        except (PermissionError, ValueError):
            status = "Эта версия больше недоступна для правки."
        with self.db:
            self.db.execute("UPDATE revision SET expires = 0 WHERE approval_id = ?", (approval_id,))
        api.call("sendMessage", chat_id=chat["id"], text=status)
        return True

    def remember(self, api: TelegramAPI, actor: TelegramActor, text: str) -> None:
        try:
            self.approvals.save_preference("telegram-demo", actor, text, self.document_id)
            reply = "Правило сохранено для этого профиля."
        except PermissionError:
            reply = "Сохранять правила может только руководитель этого профиля."
        except ValidationError:
            reply = "После /remember укажите текст правила длиной от 1 до 500 символов."
        api.call("sendMessage", chat_id=actor.telegram_user_id, text=reply)


def poll(
    api: TelegramAPI, approvals: ApprovalService, queue: EffectQueue, transport: DemoTransport,
    inbox: Path, document_id: str, plans: PlanCommands, morning: MorningPlans | None,
):
    with closing(connect(inbox)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS updates
            (id INTEGER PRIMARY KEY, body TEXT, state TEXT NOT NULL DEFAULT 'pending')""")
        revision = DemoRevision(db, approvals, document_id)
        while True:
            if morning is not None:
                morning.tick(datetime.now(UTC))
            while queue.run_once(transport) is not None:
                pass
            if transport.updates is not None and transport.updates.enqueue_latest("telegram-demo", document_id):
                continue
            cursor = db.execute("SELECT MAX(id) FROM updates").fetchone()[0]
            pending = db.execute("SELECT id, body FROM updates WHERE state = 'pending' ORDER BY id").fetchall()
            if not pending:
                try:
                    updates = api.updates(cursor + 1 if cursor is not None else None)
                except TelegramError as error:
                    if error.code in (401, 404, 409):
                        raise
                    sleep(max(2, error.retry_after))
                    continue
                with db:
                    db.executemany(
                        "INSERT OR IGNORE INTO updates (id, body) VALUES (?, ?)",
                        [(update["update_id"], dumps(update)) for update in updates
                         if isinstance(update, dict) and type(update.get("update_id")) is int],
                    )
                continue
            for update_id, body in pending:
                state = "done"
                delay = 0
                try:
                    update = loads(body)
                    if not plans.handle(update) and not revision.handle(api, update):
                        handle_update(api, approvals, update, on_revision=revision.open)
                except TelegramError as error:
                    state = "failed"
                    delay = error.retry_after
                    print(f"Событие {update_id}: {error}", flush=True)
                except (KeyError, TypeError, ValueError):
                    state = "failed"
                    print(f"Некорректное событие {update_id}", flush=True)
                with db:
                    db.execute("UPDATE updates SET state = ?, body = NULL WHERE id = ?", (state, update_id))
                if delay:
                    sleep(delay)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, help="Telegram ID согласующего")
    parser.add_argument("--chat-id", type=int, help="Чат, в который отправляется тестовая карточка")
    parser.add_argument("--get-me", action="store_true", help="Проверить токен и выйти")
    parser.add_argument("--identify", action="store_true", help="Показать ID из свежих команд /start")
    parser.add_argument("--scenario", choices=("mail", "minutes"), default="mail", help="Тип тестовой карточки")
    morning_mode = parser.add_mutually_exclusive_group()
    morning_mode.add_argument("--morning-plan", action="store_true", help="Отправлять план в 08:30 по Москве")
    morning_mode.add_argument("--send-morning-now", action="store_true", help="Отправить сегодняшний план и выйти")
    args = parser.parse_args()
    token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        parser.error("Укажите TELEGRAM_BOT_TOKEN в окружении")
    try:
        with httpx.Client(timeout=35) as client:
            api = TelegramAPI(token, client)
            bot = api.call("getMe")
            if args.get_me:
                print(f"@{bot['username']} ({bot['id']})")
                return
            webhook = api.call("getWebhookInfo")
            if webhook["url"]:
                parser.error("У бота активен webhook. Для прогона используйте отдельного тестового бота.")
            if args.identify:
                print(f"Отправьте /start в личном чате с @{bot['username']}. Жду до 25 секунд.", flush=True)
                found = False
                for update in api.updates():
                    message = update.get("message", {})
                    text = message.get("text", "")
                    if text == "/start" or text == f"/start@{bot['username']}":
                        print(f"user-id={message['from']['id']} chat-id={message['chat']['id']}")
                        found = True
                if not found:
                    parser.exit(
                        1,
                        "В полученных событиях нет команды /start. Остановите другие процессы "
                        "с этим ботом, запустите --identify снова и отправьте новую команду /start.\n",
                    )
                return
            if not args.user_id or args.user_id < 0 or args.chat_id == 0:
                parser.error("Укажите положительный --user-id и действительный --chat-id")
            morning = None
            if args.morning_plan or args.send_morning_now:
                schedule_store = Store(Path("data") / f"telegram-schedule-{bot['id']}-{args.user_id}.sqlite3")
                schedule_store.initialize()
                seed_profile(schedule_store, args.user_id)
                morning = MorningPlans(
                    schedule_store, api, "telegram-demo", args.user_id, "Europe/Moscow", build_demo_plan,
                )
            if args.send_morning_now:
                today = datetime.now(morning.timezone).date()
                effect_id = morning.enqueue(today)
                morning.drain(today)
                status = morning.queue.status(effect_id)
                print(f"План на {today.isoformat()}: {status.status}.")
                if status.status != "sent":
                    parser.exit(1)
                return
            suffix = "-minutes" if args.scenario == "minutes" else ""
            folder = Path("data") / f"telegram-demo-{bot['id']}-{args.user_id}{suffix}"
            folder.mkdir(parents=True, exist_ok=True)
            store = Store(folder / "demo.sqlite3")
            store.initialize()
            document, _ = seed_approval(store, args.user_id, args.scenario)
            approvals = ApprovalService(store)
            actor = TelegramActor(telegram_user_id=args.user_id)
            chat_id = args.chat_id if args.chat_id is not None else args.user_id

            def chat_for(profile_id, approver):
                if profile_id == "telegram-demo" and approver == actor:
                    return chat_id
                return None

            def profiles_for(user_id):
                return {"telegram-demo": "Тестовый профиль"} if user_id == args.user_id else {}

            transport = DemoTransport(
                ApprovalNoticeTransport(api, approvals, chat_for),
                DeliveryUpdates(approvals, api, chat_for),
            )
            plans = PlanCommands(api, profiles_for, build_demo_plan, bot_username=bot["username"])
            print(f"Тестовый опрос @{bot['username']}. База: {store.path}", flush=True)
            poll(
                api, approvals, EffectQueue(store), transport, folder / "inbox.sqlite3",
                document.document_id, plans, morning,
            )
    except TelegramError as error:
        if error.code == 409:
            parser.exit(1, "Telegram уже обслуживается другим опросом или webhook.\n")
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
