import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db.database import init_db
from app.services.auth_manager import AuthManager
from app.services.connection_manager import ConnectionManager
from app.services.multiplexer import MCPMultiplexer
from app.services.protocol_handler import ProtocolHandler
from app.services.registry_loader import RegistryLoader
from app.services.session_manager import SessionManager
from app.utils.id_map import IdMapper
from app.controllers.gateway_controller import get_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    # Core components (created once per process)
    registry_loader = RegistryLoader(settings.registry_path)
    connection_manager = ConnectionManager()
    auth_manager = AuthManager()
    session_manager = SessionManager(registry_loader, auth_manager, connection_manager)
    id_mapper = IdMapper()
    multiplexer = MCPMultiplexer(session_manager)
    session_manager.multiplexer = multiplexer
    protocol_handler = ProtocolHandler(session_manager, multiplexer, id_mapper)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- startup ---
        logger.info("Initializing database...")
        init_db()
        logger.info("Loading persisted sessions (best-effort)...")
        await session_manager.load_persisted_sessions()
        logger.info("Startup complete.")
        try:
            yield
        finally:
            # --- shutdown ---
            logger.info("Shutting down connection manager...")
            await connection_manager.aclose_all()

    app = FastAPI(
        title="Multi-MCP Gateway",
        description="Gateway that multiplexes multiple HTTP MCP backends into a single per-session endpoint.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Routes
    app.include_router(get_router(session_manager, protocol_handler))

    return app


app = create_app()