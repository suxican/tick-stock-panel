"""持仓研究工作区通过既有后端扩展注册。"""
from app.api.holding_analysis import router
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "holdings.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
