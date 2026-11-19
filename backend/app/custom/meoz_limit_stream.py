"""猫爪异动订阅的启停与状态入口。猫爪未配置或总开关关闭时不连接。"""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import preferences
from app.services.meoz_limit_stream import get_stream

EXTENSION_ID = "meoz.limit_stream"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


@asynccontextmanager
async def _lifespan(app):
    stream = get_stream(app.state.repo.store.data_dir)
    stream.start()
    try:
        yield
    finally:
        stream.stop()


router = APIRouter(prefix="/api/meoz-limit-stream", tags=["meoz"], lifespan=_lifespan)


@router.get("/status")
def get_status(request: Request) -> dict:
    return get_stream(request.app.state.repo.store.data_dir).status()


@router.put("/enabled")
def set_enabled(body: dict, request: Request) -> dict:
    enabled = bool(body.get("enabled"))
    preferences.save({"meoz_limit_stream_enabled": enabled})
    return get_stream(request.app.state.repo.store.data_dir).status()


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
