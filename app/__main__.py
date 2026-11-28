import argparse
import json
from pathlib import Path

import httpx

from app.adapters.model import YandexAIStudioModelPort
from app.config import Settings
from app.core.approvals import ApprovalService
from app.core.demo import seed_demo, smoke
from app.core.effects import EffectQueue, EffectStateError
from app.core.meeting_processing import process_pending_meetings
from app.core.meeting_sessions import MeetingSessions
from app.core.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Local foundation tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed-demo")
    commands.add_parser("smoke")
    retry = commands.add_parser("retry-effect", help="Requeue a delivery without calling the provider")
    retry.add_argument("effect_id")
    retry.add_argument(
        "--verified-no-delivery-by",
        help="Operator reference after confirming an unknown delivery did not occur",
    )
    provision = commands.add_parser("provision-meeting", help="Issue a private token for one meeting")
    provision.add_argument("--profile-id", required=True)
    provision.add_argument("--meeting-id", required=True)
    provision.add_argument("--initiator-id", required=True, type=int)
    provision.add_argument("--participant-ref", action="append", default=[])
    provision.add_argument("--assignee-ref", action="append", default=[])
    provision.add_argument("--previous-context-file", type=Path)
    provision.add_argument("--ttl-seconds", type=int, default=1800)
    process = commands.add_parser("process-meetings", help="Process saved transcripts for one profile")
    process.add_argument("--profile-id", required=True)
    args = parser.parse_args()

    settings = Settings.from_env()
    store = Store(settings.database_path)
    store.initialize()
    if args.command == "seed-demo":
        seed_demo(store)
        result = {"profiles": ["demo-p1", "demo-p2"]}
    elif args.command == "retry-effect":
        try:
            EffectQueue(store).retry(args.effect_id, verified_by=args.verified_no_delivery_by)
        except (LookupError, EffectStateError) as error:
            parser.error(str(error))
        result = {"effect_id": args.effect_id, "status": "pending"}
    elif args.command == "provision-meeting":
        try:
            context = args.previous_context_file.read_text(encoding="utf-8") if args.previous_context_file else None
            token = MeetingSessions(store, ApprovalService(store)).provision(
                args.profile_id, args.meeting_id, args.initiator_id,
                participant_refs=tuple(args.participant_ref), allowed_assignee_refs=tuple(args.assignee_ref),
                previous_context=context, ttl_seconds=args.ttl_seconds,
            )
        except (LookupError, OSError, ValueError) as error:
            parser.error(str(error))
        result = {"meeting_id": args.meeting_id, "token": token}
    elif args.command == "process-meetings":
        if not settings.model_api_key or not settings.model_folder_id:
            parser.error("Set YANDEX_LLM_API_KEY and YANDEX_FOLDER_ID, or run python -m app.offline_demo")
        with httpx.Client() as client:
            model = YandexAIStudioModelPort(
                settings.model_api_key, settings.model_folder_id, model=settings.model_name, client=client,
            )
            try:
                result = process_pending_meetings(store, model, profile_id=args.profile_id)
            except LookupError as error:
                parser.error(str(error))
        if result["errors"]:
            print(json.dumps(result, ensure_ascii=False))
            parser.exit(1)
    else:
        result = smoke(store)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
