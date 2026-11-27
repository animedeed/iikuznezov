from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        path = environ.get("IIKUZNEZOV_DB", "data/iikuznezov.sqlite3")
        if not path.strip():
            raise ValueError("IIKUZNEZOV_DB must be a non-empty file path")
        return cls(database_path=Path(path))
