from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.adapters.model import YandexAIStudioModelPort
from app.config import Settings
from app.core.approvals import ApprovalService
from app.core.effects import EffectQueue
from app.core.store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings = settings if settings is not None else Settings.from_env()
        store = Store(active_settings.database_path)
        store.initialize()
        application.state.store = store
        application.state.approvals = ApprovalService(store)
        application.state.effects = EffectQueue(store)
        application.state.model = None
        with httpx.Client() as client:
            if active_settings.model_api_key and active_settings.model_folder_id:
                application.state.model = YandexAIStudioModelPort(
                    active_settings.model_api_key, active_settings.model_folder_id,
                    model=active_settings.model_name, client=client, max_reply_chars=500,
                )
            yield

    application = FastAPI(
        title="ИИкузница", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
