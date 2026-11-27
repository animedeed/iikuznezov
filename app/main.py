from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings
from app.core.store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings = settings if settings is not None else Settings.from_env()
        store = Store(active_settings.database_path)
        store.initialize()
        application.state.store = store
        yield

    application = FastAPI(
        title="ИИкузница", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
