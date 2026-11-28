"""Generate a Telegram update from the durable outcome of an approval."""

from collections.abc import Callable
from hashlib import sha256
from json import dumps
from uuid import uuid4

from app.contracts import Decision, DeliveryStatus, Effect, EffectKind, EffectResult, TelegramActor
from app.core.approvals import ApprovalService
from app.core.effects import enqueue_effect
from app.telegram.bot import TelegramAPI, _pages, deliver_pages


_KINDS = {"email": "Отправка", "task": "Создание поручения", "calendar_event": "Событие календаря"}
_STATES = {
    "sent": "выполнено",
    "retryable": "ожидает повтора",
    "failed": "ошибка, требуется вмешательство",
    "unknown": "исход неизвестен, проверьте у провайдера перед повтором",
    "cancelled": "отменено",
}


class DeliveryUpdates:
    def __init__(
        self, approvals: ApprovalService, api: TelegramAPI,
        chat_for: Callable[[str, TelegramActor], int | None],
    ):
        self.approvals = approvals
        self.api = api
        self.chat_for = chat_for

    def enqueue_latest(self, profile_id: str, document_id: str) -> bool:
        with self.approvals.store._connection(write=True) as db:
            approval = db.execute(
                """SELECT a.approval_id, a.version, a.approver_telegram_id
                   FROM approvals AS a
                   WHERE a.profile_id = ? AND a.document_id = ? AND a.state = 'approved'
                   AND a.version = (SELECT MAX(d.version) FROM document_versions AS d
                       WHERE d.profile_id = a.profile_id AND d.document_id = a.document_id)""",
                (profile_id, document_id),
            ).fetchone()
            if approval is None:
                return False
            approval_id, version, user_id = approval
            operations = db.execute(
                """SELECT effect_id, kind, target_ref, status FROM effects
                   WHERE profile_id = ? AND document_id = ? AND version = ?
                   AND kind IN ('email', 'task', 'calendar_event') ORDER BY kind, target_ref, effect_id""",
                (profile_id, document_id, version),
            ).fetchall()
            if not operations or any(operation["status"] not in _STATES for operation in operations):
                return False
            snapshot = dumps([tuple(operation) for operation in operations], ensure_ascii=False)
            key = f"delivery-update:{approval_id}:{sha256(snapshot.encode()).hexdigest()}"
            if db.execute(
                "SELECT 1 FROM effects WHERE profile_id = ? AND key = ?", (profile_id, key),
            ).fetchone():
                return False
            lines = [f"Результат выполнения · версия {version}"]
            for operation in operations:
                lines.append(
                    f"• {_KINDS[operation['kind']]} ({operation['target_ref']}) — {_STATES[operation['status']]}"
                )
            states = {operation["status"] for operation in operations}
            if "retryable" in states:
                lines.append("\nБот повторит временно неудавшиеся действия автоматически.")
            if states & {"unknown", "failed"}:
                lines.append("\nПередайте этот результат администратору. Повторно утверждать карточку не нужно.")
            db.execute(
                """UPDATE effects SET status = 'cancelled'
                   WHERE profile_id = ? AND document_id = ? AND version = ?
                   AND kind = 'telegram' AND target_ref = ?
                   AND json_extract(payload, '$.operation') = 'delivery_update'
                   AND status IN ('pending', 'retryable')""",
                (profile_id, document_id, version, approval_id),
            )
            enqueue_effect(db, Effect(
                effect_id=uuid4().hex, profile_id=profile_id, document_id=document_id, version=version,
                key=key, kind=EffectKind.TELEGRAM, target_ref=approval_id,
                payload={"operation": "delivery_update", "telegram_user_id": user_id, "text": "\n".join(lines)},
            ))
            return True

    def execute(self, effect: Effect) -> EffectResult:
        actor = TelegramActor(telegram_user_id=effect.payload["telegram_user_id"])
        decision = self.approvals.get_decision(effect.profile_id, effect.target_ref, actor)
        chat_id = self.chat_for(effect.profile_id, actor)
        if (
            decision is None or decision.decision != Decision.APPROVE or chat_id is None
            or decision.document_id != effect.document_id or decision.version != effect.version
        ):
            return EffectResult(status=DeliveryStatus.FAILED, error_code="delivery_update_not_authorized")
        return deliver_pages(self.api, chat_id, _pages(effect.payload["text"]))
