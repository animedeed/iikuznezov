from collections.abc import Callable
from typing import Any

import httpx

from app.contracts import (
    ApprovalCardView,
    Decision,
    DecisionStatus,
    DeliveryStatus,
    Effect,
    EffectKind,
    EffectResult,
    TelegramActor,
)
from app.core.approvals import ApprovalService
from app.ports import ApprovalPort


class TelegramError(RuntimeError):
    def __init__(self, code: int | None, retry_after: int = 0):
        super().__init__(f"Telegram API error ({code or 'unknown'})")
        self.code = code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, client: httpx.Client):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client

    def call(self, method: str, **payload: Any) -> Any:
        try:
            response = self._client.post(f"{self._base_url}/{method}", json=payload)
            data = response.json()
        except (httpx.RequestError, ValueError):
            # Request exceptions contain the URL and therefore the bot token.
            raise TelegramError(None) from None
        if not isinstance(data, dict):
            raise TelegramError(None)
        if response.is_success and data.get("ok") is True and "result" in data:
            return data["result"]
        code = data.get("error_code", response.status_code)
        parameters = data.get("parameters")
        retry_after = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        raise TelegramError(
            code if type(code) is int else None,
            min(retry_after, 86_400) if type(retry_after) is int and retry_after > 0 else 0,
        )

    def updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates", offset=offset, timeout=25,
            allowed_updates=["message", "callback_query"],
        )


_BUTTONS = {
    Decision.APPROVE: ("a", "Утвердить"),
    Decision.REJECT: ("r", "Отклонить"),
    Decision.REVISE: ("v", "На правку"),
}
_DECISIONS = {code: decision for decision, (code, _) in _BUTTONS.items()}
_STATUS_TEXT = {
    DecisionStatus.ALREADY_DECIDED: "Решение уже сохранено.",
    DecisionStatus.STALE: "Карточка устарела.",
    DecisionStatus.FORBIDDEN: "У вас нет доступа к этому согласованию.",
    DecisionStatus.VALIDATION_ERROR: "Не удалось принять решение.",
}
_ACCEPTED_TEXT = {
    Decision.APPROVE: "Утверждено. Результат доставки будет отдельно.",
    Decision.REJECT: "Отклонено.",
    Decision.REVISE: "Отправлено на правку.",
}


def _pages(message: str, limit: int = 3500) -> list[str]:
    # Bound the size in UTF-16 units even for text containing emoji.
    pages = []
    while message:
        end = min(len(message), limit)
        while len(message[:end].encode("utf-16-le")) > limit * 2:
            end -= 1
        if end < len(message):
            newline = message.rfind("\n", 0, end)
            if newline > end // 2:
                end = newline
        pages.append(message[:end])
        message = message[end:].lstrip("\n")
    return pages


def render_card(card: ApprovalCardView) -> tuple[list[str], dict[str, Any]]:
    document = card.document
    candidate = document.candidate
    if candidate.kind == "mail_action":
        dispatch = document.mail_dispatch
        action = {"forward": "Переслать", "reply": "Ответить", "calendar_change": "Изменить календарь"}
        destination = (
            f"Действие: {action[dispatch.action]}\nАдресат: {dispatch.target_ref}"
            if dispatch else "Адресат требует уточнения."
        )
        message = (
            f"Согласование письма · версия {document.version}\n{destination}\n"
            f"Тема запроса: {candidate.topic}\nОснование: {candidate.rationale}\n\n"
            f"{candidate.draft_text or 'Черновик отсутствует.'}"
        )
    else:
        decisions = "\n".join(f"• {item.text}" for item in candidate.decisions)
        tasks = "\n".join(
            f"• {task.action} — {task.assignee_ref or 'исполнитель не указан'}, "
            f"{task.due_date or 'срок не указан'}"
            for task in candidate.tasks
        )
        message = (
            f"{candidate.title} · версия {document.version}\n"
            f"Адресаты: {', '.join(candidate.participant_refs) or 'не указаны'}\n\n{candidate.summary}\n\n"
            f"Решения\n{decisions or 'Нет решений'}\n\nПоручения\n{tasks or 'Нет поручений'}"
        )
    buttons = []
    for decision in card.request.actions:
        code, label = _BUTTONS[decision]
        value = f"{code}:{card.request.approval_id}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("Approval ID does not fit in Telegram callback data")
        buttons.append({"text": label, "callback_data": value})
    return _pages(message), {"inline_keyboard": [buttons]}


def deliver_pages(
    api: TelegramAPI, chat_id: int, pages: list[str], *, markup: dict[str, Any] | None = None,
) -> EffectResult:
    sent_pages = 0
    try:
        for index, page in enumerate(pages):
            options = {"reply_markup": markup} if markup and index == len(pages) - 1 else {}
            message = api.call("sendMessage", chat_id=chat_id, text=page, **options)
            sent_pages += 1
    except TelegramError as error:
        if sent_pages or error.code is None or error.code >= 500:
            return EffectResult(status=DeliveryStatus.UNKNOWN, error_code="telegram_delivery_unknown")
        if error.code == 429:
            return EffectResult(
                status=DeliveryStatus.RETRYABLE, error_code="telegram_rate_limit",
                retry_after_seconds=error.retry_after,
            )
        return EffectResult(status=DeliveryStatus.FAILED, error_code="telegram_rejected")
    return EffectResult(status=DeliveryStatus.SENT, external_id=str(message["message_id"]))


class ApprovalNoticeTransport:
    def __init__(
        self, api: TelegramAPI, approvals: ApprovalService,
        chat_for: Callable[[str, TelegramActor], int | None],
        record_card: Callable[[str, str, TelegramActor, int, int], None] | None = None,
    ):
        self.api = api
        self.approvals = approvals
        self.chat_for = chat_for
        self.record_card = record_card

    def execute(self, effect: Effect) -> EffectResult:
        if effect.kind != EffectKind.TELEGRAM or effect.payload.get("operation") != "approval_notice":
            return EffectResult(status=DeliveryStatus.FAILED, error_code="unsupported_effect")
        actor = TelegramActor(telegram_user_id=effect.payload["telegram_user_id"])
        card = self.approvals.get_pending(effect.profile_id, effect.target_ref, actor)
        if card is None:
            return EffectResult(status=DeliveryStatus.FAILED, error_code="card_unavailable")
        chat_id = self.chat_for(effect.profile_id, actor)
        if chat_id is None:
            return EffectResult(status=DeliveryStatus.FAILED, error_code="chat_unbound")
        pages, markup = render_card(card)
        result = deliver_pages(self.api, chat_id, pages, markup=markup)
        if result.status == DeliveryStatus.SENT and self.record_card is not None:
            self.record_card(effect.target_ref, effect.profile_id, actor, chat_id, int(result.external_id))
        return result


def handle_update(
    api: TelegramAPI, approvals: ApprovalPort, update: dict[str, Any],
    *, on_revision: Callable[[str, TelegramActor], str | None] | None = None,
    check_card: Callable[[str, int, int], bool] | None = None,
) -> None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        message = update.get("message")
        if isinstance(message, dict) and message.get("text") == "/start":
            chat = message.get("chat")
            if isinstance(chat, dict) and chat.get("type") == "private" and type(chat.get("id")) is int:
                api.call("sendMessage", chat_id=chat["id"], text="Бот готов принимать согласования.")
        return
    callback_id = callback.get("id")
    if not isinstance(callback_id, str) or not callback_id:
        return
    sender = callback.get("from")
    user_id = sender.get("id") if isinstance(sender, dict) else None
    data = callback.get("data")
    code, separator, approval_id = data.partition(":") if isinstance(data, str) else ("", "", "")
    decision = _DECISIONS.get(code)
    if (
        not separator or not approval_id or not approval_id.isascii() or len(data) > 64
        or decision is None or type(user_id) is not int or user_id <= 0
    ):
        api.call("answerCallbackQuery", callback_query_id=callback_id, text="Некорректная кнопка.")
        return
    message = callback.get("message")
    if check_card is not None:
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if type(chat_id) is not int or type(message_id) is not int or not check_card(approval_id, chat_id, message_id):
            api.call("answerCallbackQuery", callback_query_id=callback_id, text="Карточка недоступна.", show_alert=True)
            return
    actor = TelegramActor(telegram_user_id=user_id)
    status = approvals.decide(approval_id, actor, decision)
    accepted = status == DecisionStatus.ACCEPTED
    text = _ACCEPTED_TEXT[decision] if accepted else _STATUS_TEXT[status]
    if on_revision is not None and decision == Decision.REVISE and (
        accepted or status == DecisionStatus.ALREADY_DECIDED
    ):
        prompt = on_revision(approval_id, actor)
        if accepted and prompt:
            text = prompt
    answer = text if len(text) <= 180 else "Сессия правки открыта. Команды указаны в сообщении."
    api.call("answerCallbackQuery", callback_query_id=callback_id, text=answer, show_alert=not accepted)
    if accepted and isinstance(message, dict):
        chat = message.get("chat")
        if isinstance(chat, dict) and type(chat.get("id")) is int and type(message.get("message_id")) is int:
            api.call(
                "editMessageReplyMarkup", chat_id=chat["id"], message_id=message["message_id"],
                reply_markup={"inline_keyboard": []},
            )
            api.call("sendMessage", chat_id=chat["id"], text=text)
