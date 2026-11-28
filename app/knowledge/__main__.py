import argparse
import json
from dataclasses import asdict
from pathlib import Path

import httpx

from app.adapters.model import ModelAdapterError, YandexAIStudioModelPort
from app.config import Settings
from app.core.store import Store
from app.knowledge import MAX_DOCUMENT_CHARS, KnowledgeBase


def main() -> None:
    parser = argparse.ArgumentParser(description="Work with one profile's local knowledge base")
    parser.add_argument("--profile-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Index or replace one UTF-8 text or Markdown source")
    index.add_argument("source_id")
    index.add_argument("path", type=Path)
    commands.add_parser("search").add_argument("query")
    commands.add_parser("ask").add_argument("question")
    args = parser.parse_args()

    try:
        settings = Settings.from_env()
        store = Store(settings.database_path)
        store.initialize()
        knowledge = KnowledgeBase(store)
        if args.command == "index":
            with args.path.open(encoding="utf-8") as source:
                count = knowledge.index(args.profile_id, args.source_id, source.read(MAX_DOCUMENT_CHARS + 1))
            result = {"indexed_chunks": count}
        elif args.command == "search":
            result = {"passages": [asdict(passage) for passage in knowledge.search(args.profile_id, args.query)]}
        else:
            if not settings.model_api_key or not settings.model_folder_id:
                parser.error("Set YANDEX_LLM_API_KEY and YANDEX_FOLDER_ID to generate an answer")
            with httpx.Client() as client:
                model = YandexAIStudioModelPort(
                    settings.model_api_key, settings.model_folder_id,
                    model=settings.model_name, client=client,
                )
                answer, citations = knowledge.answer(args.profile_id, args.question, model)
            result = {
                "answer": answer,
                "sources": [
                    {"source_id": passage.source_id, "position": passage.position}
                    for passage in citations
                ],
            }
    except (LookupError, OSError, ValueError, ModelAdapterError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
