"""通过已存在的扩展注册入口接入回春模式。"""
from app.api.huichun import router
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "huichun.workbench"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
