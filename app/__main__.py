import argparse
import json

from app.config import Settings
from app.core.demo import seed_demo, smoke
from app.core.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Local foundation tools")
    parser.add_argument("command", choices=("seed-demo", "smoke"))
    args = parser.parse_args()
    store = Store(Settings.from_env().database_path)
    store.initialize()
    if args.command == "seed-demo":
        seed_demo(store)
        result = {"profiles": ["demo-p1", "demo-p2"]}
    else:
        result = smoke(store)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
