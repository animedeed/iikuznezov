"""Version 1 types shared by the service and channel adapters."""

from datetime import UTC, date
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, StringConstraints, field_validator, model_validator

CONTRACT_VERSION = 1
UtcMoment = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Route(StrEnum):
    FORWARD_SALES = "forward_sales"
    FORWARD_SERVICE = "forward_service"
    RECORD_STATUS = "record_status"
    IGNORE = "ignore"
    REVIEW = "review"
    CHECK_CALENDAR = "check_calendar"


class PolicyClass(StrEnum):
    FINANCE = "finance"
    HR = "hr"
    CUSTOMER_COMMITMENT = "customer_commitment"
    EXECUTIVE_MAIL = "executive_mail"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class DecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_DECIDED = "already_decided"
    STALE = "stale"
    FORBIDDEN = "forbidden"
    VALIDATION_ERROR = "validation_error"


class EffectKind(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    TASK = "task"
    CALENDAR_EVENT = "calendar_event"


class DeliveryStatus(StrEnum):
    SENT = "sent"
    RETRYABLE = "retryable"
    UNKNOWN = "unknown"
    FAILED = "failed"


class Profile(Contract):
    profile_id: Ref
    timezone: str
    quote_route: Route

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be an IANA zone") from error
        return value


class MailReceipt(Contract):
    folder: Ref
    uidvalidity: PositiveInt
    uid: PositiveInt
    sender: Annotated[str, StringConstraints(min_length=1, max_length=320)] = Field(alias="from")
    subject: str = Field(max_length=998)
    text: str = Field(max_length=100_000)
    received_at: UtcMoment


class ReceiptResult(Contract):
    event_id: Ref
    profile_id: Ref
    duplicate: bool


class MailCandidate(Contract):
    kind: Literal["mail_action"] = "mail_action"
    topic: Ref
    requires_review: bool
    rationale: ShortText
    policy_class: PolicyClass | None = None
    draft_text: MessageText | None = None


class MeetingTurn(Contract):
    request_id: Ref
    sequence: PositiveInt
    text: MessageText
    role: Literal["participant"] = "participant"


class MeetingReply(Contract):
    sequence: PositiveInt
    reply_text: ShortText
    approval_required: bool


class SourcedText(Contract):
    text: ShortText
    source_turn_ids: tuple[Ref, ...] = ()


class TaskDraft(Contract):
    task_ref: Ref
    action: ShortText
    assignee_ref: Ref | None
    due_date: date | None
    status: Literal["draft", "needs_clarification"]
    source_turn_ids: tuple[Ref, ...] = ()


class MinutesCandidate(Contract):
    kind: Literal["minutes"] = "minutes"
    meeting_id: Ref
    title: ShortText
    occurred_at: UtcMoment
    participant_refs: tuple[Ref, ...]
    summary: MessageText
    decisions: tuple[SourcedText, ...]
    open_questions: tuple[SourcedText, ...]
    tasks: tuple[TaskDraft, ...]


Candidate = Annotated[MailCandidate | MinutesCandidate, Field(discriminator="kind")]


class MailDispatch(Contract):
    """Server-resolved action and target bound to a document version."""

    action: Literal["forward", "reply", "calendar_change"]
    target_ref: Ref


class DocumentVersion(Contract):
    profile_id: Ref
    document_id: Ref
    source_id: Ref
    version: PositiveInt
    candidate: Candidate
    mail_dispatch: MailDispatch | None = None

    @model_validator(mode="after")
    def validate_dispatch(self) -> "DocumentVersion":
        if self.mail_dispatch is not None and self.candidate.kind != "mail_action":
            raise ValueError("Only mail documents can have a mail dispatch")
        return self


class TelegramActor(Contract):
    telegram_user_id: PositiveInt


class ApprovalRequest(Contract):
    approval_id: Ref
    profile_id: Ref
    document_id: Ref
    version: PositiveInt
    actor_ref: Ref
    status: Literal["pending_approval"] = "pending_approval"
    actions: tuple[Decision, ...] = (Decision.APPROVE, Decision.REJECT, Decision.REVISE)


class ApprovalCardView(Contract):
    request: ApprovalRequest
    document: DocumentVersion

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ApprovalCardView":
        expected = (self.request.profile_id, self.request.document_id, self.request.version)
        actual = (self.document.profile_id, self.document.document_id, self.document.version)
        if expected != actual:
            raise ValueError("Approval must reference this exact document version")
        return self


class Effect(Contract):
    effect_id: Ref
    profile_id: Ref
    key: str = Field(min_length=1, max_length=512)
    kind: EffectKind
    target_ref: Ref
    document_id: Ref | None = None
    version: PositiveInt | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class EffectResult(Contract):
    status: DeliveryStatus
    external_id: Ref | None = None
    error_code: Ref | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)


class PlanMeeting(Contract):
    id: Ref
    title: ShortText
    starts_at: UtcMoment | None = None


class PlanTask(Contract):
    id: Ref
    title: ShortText
    due_date: date


class Plan(Contract):
    profile_id: Ref
    date: date
    period: Literal["day", "week"]
    timezone: str
    meetings: tuple[PlanMeeting, ...]
    tasks_due: tuple[PlanTask, ...]
    overdue: tuple[PlanTask, ...]
    previous_workday: date
    previous_workday_results: tuple[ShortText, ...]
    mail_statuses: tuple[ShortText, ...]
    pending_approvals: tuple[Ref, ...]
