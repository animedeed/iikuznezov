import argparse
import json

from app.config import Settings
from app.core.demo import seed_demo, smoke
from app.core.effects import EffectQueue, EffectStateError
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
    args = parser.parse_args()
    store = Store(Settings.from_env().database_path)
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
    else:
        result = smoke(store)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
