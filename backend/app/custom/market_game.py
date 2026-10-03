"""Optional workbench, registered without altering core navigation or lifecycle."""
from app.api.market_game import router
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "market-game.workbench"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
