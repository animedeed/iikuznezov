"""One profile's orchestration across mail, meetings, calendar and Telegram."""

from datetime import UTC, datetime
from logging import getLogger

from app.adapters.model import ModelAdapterError
from app.calendar.ports import CalendarPermanentError, CalendarTemporaryError
from app.contracts import Decision, DeliveryStatus, Effect, EffectKind, EffectResult, MailCandidate, TelegramActor
from app.core.approvals import EffectIntent
from app.core.effects import EffectQueue
from app.core.mail_polling import MailPolling
from app.core.meeting_processing import process_pending_meetings
from app.core.store import Store
from app.knowledge import KnowledgeBase
from app.mail.imap import MailPollError
from app.ports import EffectTransport, ModelPort
from app.telegram.bot import _pages, deliver_pages
from app.telegram.channel import TelegramChannel
from app.telegram.demo_minutes import revise_minutes
from app.workflow.followups import load_meeting_source, minutes_intents, sweep_approved_minutes
from app.workflow.plan import send_due_plans
from app.workflow.transport import WorkflowTransport


logger = getLogger(__name__)

_HELP_TEXT = (
    "Я помогаю согласовывать письма и протоколы. Когда потребуется решение, пришлю карточку с кнопками.\n\n"
    "План дня: /plan 2026-12-07\n"
    "План недели: /week 2026-12-07\n"
    "Вопрос по базе знаний: /ask Какие сроки мы согласовали?\n\n"
    "В планах замените дату на нужную. Для правки нажмите «На правку»: я пришлю команду с примером."
)


class PilotWorker:
    def __init__(
        self, store: Store, profile_id: str, telegram: TelegramChannel, workflow: WorkflowTransport, *,
        smtp: EffectTransport, mail: MailPolling, model: ModelPort,
        recipient_fingerprints: dict[str, str], bot_username: str,
    ):
        self.store = store
        self.profile_id = profile_id
        self.telegram = telegram
        self.workflow = workflow
        self.smtp = smtp
        self.mail = mail
        self.model = model
        self.fingerprints = recipient_fingerprints
        self.bot_username = bot_username.casefold()
        self.queue = EffectQueue(store)
        self.knowledge = KnowledgeBase(store)

    def tick(self) -> None:
        try:
            self.mail.tick()
        except MailPollError as error:
            logger.warning("Mail polling paused: %s", error)

        meetings = process_pending_meetings(
            self.store, self.model, profile_id=self.profile_id,
            recipient_fingerprints=self.fingerprints,
        )
        for event in meetings["errors"]:
            logger.warning("Meeting processing failed id=%s code=%s", event["event_id"], event["error_code"])
        sweep_approved_minutes(self.store, profile_id=self.profile_id)
        try:
            send_due_plans(
                datetime.now(UTC), store=self.store, calendar=self.workflow.calendar,
                tasks=self.workflow.tasks, profile_ids=(self.profile_id,),
            )
        except (CalendarTemporaryError, CalendarPermanentError) as error:
            logger.warning("Plan scheduling paused: %s", error.code)
        for _ in range(100):
            if self.queue.run_once(self, profile_id=self.profile_id, lease_seconds=180) is None:
                break
        with self.store._connection() as db:
            documents = db.execute(
                "SELECT DISTINCT document_id FROM approvals WHERE profile_id=? AND state='approved'",
                (self.profile_id,),
            ).fetchall()
        for document in documents:
            self.telegram.delivery_updates.enqueue_latest(self.profile_id, document["document_id"])

    def execute(self, effect: Effect) -> EffectResult:
        if effect.profile_id != self.profile_id:
            return EffectResult(status=DeliveryStatus.FAILED, error_code="unbound_profile")
        if effect.kind == EffectKind.EMAIL:
            return self.smtp.execute(effect)
        if effect.kind in {EffectKind.TASK, EffectKind.CALENDAR_EVENT}:
            return self.workflow.execute(effect)
        if effect.payload.get("operation") != "plan_message":
            return self.telegram.execute(effect)
        user_id = effect.payload.get("telegram_user_id")
        text = effect.payload.get("text")
        if type(user_id) is not int or user_id <= 0 or not isinstance(text, str) or not text.strip():
            return EffectResult(status=DeliveryStatus.FAILED, error_code="invalid_plan_message")
        if self.telegram.chat_for(self.profile_id, TelegramActor(telegram_user_id=user_id)) != user_id:
            return EffectResult(status=DeliveryStatus.FAILED, error_code="plan_recipient_changed")
        return deliver_pages(self.telegram.api, user_id, _pages(text))

    def handle_update(self, update: dict) -> None:
        message = update.get("message")
        text = message.get("text") if isinstance(message, dict) else None
        if isinstance(text, str) and text.strip():
            args = text.split(maxsplit=1)
            command, _, username = args[0].partition("@")
            if command in {"/start", "/help", "/ask", "/revise", "/remember"} and (
                not username or username.casefold() == self.bot_username
            ):
                self._handle_private_command(message, command, args[1] if len(args) == 2 else "")
                return
        self.telegram.handle_update(update, on_revision=self._revision_prompt)

    def _handle_private_command(self, message: dict, command: str, argument: str) -> None:
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        user_id = sender.get("id")
        if type(user_id) is not int or user_id <= 0 or chat.get("id") != user_id or chat.get("type") != "private":
            return
        actor = TelegramActor(telegram_user_id=user_id)
        if command in {"/start", "/help"}:
            reply = _HELP_TEXT
        elif command == "/ask":
            if not self.telegram.plans.profiles_for(user_id).get(self.profile_id):
                reply = "У вас нет доступа к базе знаний. Если он нужен, обратитесь к администратору профиля."
            else:
                reply = self._answer(argument)
        elif command == "/revise":
            reply = self._revise(actor, argument)
        else:
            document_id, _, preference = argument.partition(" ")
            try:
                self.telegram.approvals.save_preference(self.profile_id, actor, preference, document_id)
            except PermissionError:
                reply = "Сохранять правила может только руководитель профиля."
            except (LookupError, ValueError):
                reply = "Скопируйте команду /remember из сообщения после «На правку» и подставьте текст правила."
            else:
                reply = "Правило сохранено для этого профиля."
        for page in _pages(reply):
            self.telegram.api.call("sendMessage", chat_id=user_id, text=page)

    def _answer(self, question: str) -> str:
        try:
            answer, sources = self.knowledge.answer(self.profile_id, question, self.model)
        except ValueError:
            return "Введите /ask и вопрос длиной до 1000 символов."
        except ModelAdapterError:
            return "Модель временно недоступна. Повторите запрос позже."
        if sources:
            answer += "\n\nИсточники\n" + "\n".join(
                f"• {source.source_id} (фрагмент {source.position})" for source in sources
            )
        return answer

    def _revision_prompt(self, approval_id: str, actor: TelegramActor) -> str | None:
        decision = self.telegram.approvals.get_decision(self.profile_id, approval_id, actor)
        if decision is None or decision.decision != Decision.REVISE:
            return None
        document = self.store.get_version(self.profile_id, decision.document_id, decision.version)
        if document is None:
            return None
        command = f"/revise {approval_id}"
        if isinstance(document.candidate, MailCandidate):
            instructions = f"{command} Новый текст письма"
            if document.mail_dispatch and document.mail_dispatch.action == "forward":
                instructions = f"{command} Комментарий к пересылаемому письму"
        else:
            source, _ = load_meeting_source(self.store, self.profile_id, document.source_id)
            examples = [f"{command} summary Новый итог встречи"]
            if document.candidate.tasks:
                examples += [f"{command} task Новый текст поручения", f"{command} due 2026-12-08"]
                if source.allowed_assignee_refs:
                    examples.append(f"{command} assignee {source.allowed_assignee_refs[0]}")
            if source.participant_refs:
                examples.append(f"{command} recipient {source.participant_refs[0]}")
            instructions = "\n".join(examples)
        prompt = f"Отправьте одну команду в личном чате со мной. После правки я пришлю новую карточку.\n\n{instructions}"
        if self.telegram.plans.profiles_for(actor.telegram_user_id).get(self.profile_id):
            prompt += f"\n\nЧтобы сохранить постоянное правило профиля:\n/remember {document.document_id} Текст правила"
        return prompt

    def _revise(self, actor: TelegramActor, argument: str) -> str:
        approval_id, _, text = argument.partition(" ")
        approvals = self.telegram.approvals
        decision = approvals.get_decision(self.profile_id, approval_id, actor)
        if decision is None or decision.decision != Decision.REVISE or not text.strip():
            return "Нажмите «На правку» на актуальной карточке и скопируйте команду из моего сообщения."
        document = self.store.get_version(self.profile_id, decision.document_id, decision.version)
        if document is None:
            return "Версия недоступна."
        try:
            if isinstance(document.candidate, MailCandidate):
                candidate = MailCandidate.model_validate({**document.candidate.model_dump(), "draft_text": text})
                ref = document.mail_dispatch.target_ref if document.mail_dispatch else None
                fingerprint = self.fingerprints.get(ref) if ref else None
                intents = (EffectIntent(
                    kind=EffectKind.EMAIL, target_ref=ref, recipient_fingerprint=fingerprint,
                ),) if fingerprint else ()
            else:
                source, _ = load_meeting_source(self.store, self.profile_id, document.source_id)
                candidate = revise_minutes(
                    document.candidate, text, assignees=source.allowed_assignee_refs, recipients=source.participant_refs,
                )
                intents = minutes_intents(candidate, source, recipient_fingerprints=self.fingerprints)
            approvals.revise_document(
                self.profile_id, document.document_id, actor, decision.version, candidate,
                mail_dispatch=document.mail_dispatch, intents=intents,
            )
        except (LookupError, ValueError, PermissionError):
            return "Правка не сохранена. Проверьте команду по примеру в чате и версию карточки."
        return "Правка сохранена. Бот пришлёт карточку новой версии."
