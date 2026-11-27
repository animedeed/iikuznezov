from dataclasses import dataclass, field
from os import environ
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    model_api_key: str | None = field(default=None, repr=False)
    model_folder_id: str | None = None
    model_name: str = "yandexgpt-5.1"

    def __post_init__(self) -> None:
        if bool(self.model_api_key) != bool(self.model_folder_id):
            raise ValueError("Set both YANDEX_LLM_API_KEY and YANDEX_FOLDER_ID to enable the model")

    @classmethod
    def from_env(cls) -> "Settings":
        path = environ.get("IIKUZNEZOV_DB", "data/iikuznezov.sqlite3")
        if not path.strip():
            raise ValueError("IIKUZNEZOV_DB must be a non-empty file path")
        return cls(
            database_path=Path(path),
            model_api_key=environ.get("YANDEX_LLM_API_KEY", "").strip() or None,
            model_folder_id=environ.get("YANDEX_FOLDER_ID", "").strip() or None,
            model_name=environ.get("YANDEX_LLM_MODEL", "").strip() or "yandexgpt-5.1",
        )
