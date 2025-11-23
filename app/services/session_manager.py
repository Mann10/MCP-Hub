import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List , Optional, FrozenSet , Any
from uuid import UUID

from sqlmodel import select

from app.db.database import engine, get_db_session
from app.db.models import MCPGatewaySession
from .auth_manager import AuthManager
from .connection_manager import ConnectionManager, BackendHandle
from .registry_loader import RegistryLoader, ProviderConfig

logger = logging.getLogger(__name__)


@dataclass
class RuntimeSessionState:
    connections: Dict[str, BackendHandle] = field(default_factory=dict)
    # Mapping: "provider.tool" -> {"provider": provider_name, "backend_tool_name": original_name}
    tool_name_map: Dict[str, Dict[str, str]] = field(default_factory=dict)
    provider_session_headers: Dict[str, Dict[str, str]] = field(default_factory=dict)
    cached_tools: Optional[Dict[str, Any]] = None
    cached_tools_ts: Optional[float] = None
    cached_tools_providers: Optional[FrozenSet[str]] = None

class SessionManager:
    """
    Orchestrates session lifecycle and maintains in-memory runtime state.
    """

    def __init__(
        self,
        registry_loader: RegistryLoader,
        auth_manager: AuthManager,
        connection_manager: ConnectionManager,
    ) -> None:
        self.registry_loader = registry_loader
        self.auth_manager = auth_manager
        self.connection_manager = connection_manager
        # session_id -> RuntimeSessionState
        self._runtime_sessions: Dict[UUID, RuntimeSessionState] = {}

    # ---------- Persistence helpers ----------

    def _persist_session(
        self,
        servers: List[ProviderConfig],
        credentials: Dict,
        state: str = "ready",
    ) -> MCPGatewaySession:
        servers_payload = [
            {
                "name": s.name,
                "protocol": s.protocol,
                "rpc_endpoint": s.rpc_endpoint,
                "auth_type": s.auth_type,
                "api_key_header_name": s.api_key_header_name,
            }
            for s in servers
        ]
        now = datetime.utcnow()

        with get_db_session() as db:
            session_obj = MCPGatewaySession(
                created_at=now,
                updated_at=now,
                state=state,
                servers_json=json.dumps(servers_payload),
                credentials_json=json.dumps(credentials),
            )
            db.add(session_obj)
            db.commit()
            db.refresh(session_obj)
            return session_obj

    def _load_session_model(self, session_id: UUID) -> MCPGatewaySession | None:
        with get_db_session() as db:
            stmt = select(MCPGatewaySession).where(MCPGatewaySession.id == session_id)
            return db.exec(stmt).first()

    def _update_state(self, session_id: UUID, state: str) -> None:
        with get_db_session() as db:
            stmt = select(MCPGatewaySession).where(MCPGatewaySession.id == session_id)
            sess = db.exec(stmt).first()
            if not sess:
                return
            sess.state = state
            sess.updated_at = datetime.utcnow()
            db.add(sess)
            db.commit()

    # ---------- Runtime helpers ----------

    async def _build_runtime_state(self, db_session: MCPGatewaySession) -> RuntimeSessionState:
        servers = json.loads(db_session.servers_json)
        credentials = json.loads(db_session.credentials_json)
        runtime = RuntimeSessionState(connections={}, tool_name_map={})

        for s in servers:
            name = s["name"]
            provider_cfg = self.registry_loader.get_provider_config(name)
            creds_for_provider = credentials.get(name, {})
            headers = self.auth_manager.build_headers(provider_cfg, creds_for_provider)

            handle = await self.connection_manager.get_or_create_handle(
                db_session.id,
                name,
                provider_cfg.rpc_endpoint,
                headers,
            )
            runtime.connections[name] = handle

        self._runtime_sessions[db_session.id] = runtime
        print(f'Runtime is {runtime}')
        return runtime

    # ---------- Public API ----------

    async def create_session(self, servers: List[str], credentials: Dict) -> MCPGatewaySession:
        """
        Create a new session: validate providers, build auth headers, create connections,
        persist into DB, initialize runtime state.
        """
        # Validate providers + load configs
        provider_cfgs: List[ProviderConfig] = []
        for name in servers:
            try:
                cfg = self.registry_loader.get_provider_config(name)
            except KeyError as e:
                raise ValueError(f"Unknown provider '{name}'") from e
            if cfg.protocol.lower() != "http":
                # Future extension: handle websockets
                raise ValueError(
                    f"Provider '{name}' uses unsupported protocol '{cfg.protocol}'. "
                    "Only 'http' is supported for now."
                )
            provider_cfgs.append(cfg)

        # Persist
        db_session = self._persist_session(provider_cfgs, credentials, state="ready")

        # Build runtime (best-effort; if it fails, mark state failed)
        try:
            await self._build_runtime_state(db_session)
        except Exception:
            logger.exception("Failed to create runtime state for session %s", db_session.id)
            self._update_state(db_session.id, "failed")
            raise

        return db_session

    async def load_persisted_sessions(self) -> None:
        """
        On startup, load all 'ready' sessions from DB and re-create runtime connections.

        Best-effort: failures are logged but do not abort startup.
        """
        with get_db_session() as db:
            stmt = select(MCPGatewaySession).where(MCPGatewaySession.state == "ready")
            sessions = db.exec(stmt).all()

        for sess in sessions:
            try:
                await self._build_runtime_state(sess)
                logger.info("Restored runtime state for session %s", sess.id)
            except Exception:
                logger.exception("Failed to restore runtime state for session %s", sess.id)

    def ensure_session_exists(self, session_id: UUID) -> MCPGatewaySession:
        sess = self._load_session_model(session_id)
        if not sess:
            raise KeyError(f"Session {session_id} not found")
        return sess

    async def get_runtime_state(self, session_id: UUID) -> RuntimeSessionState:
        """
        Ensure runtime state exists; if not, re-create from persisted data.
        """
        if session_id in self._runtime_sessions:
            return self._runtime_sessions[session_id]

        sess = self._load_session_model(session_id)
        if not sess:
            raise KeyError(f"Session {session_id} not found")
        return await self._build_runtime_state(sess)

    def get_session_info(self, session_id: UUID) -> MCPGatewaySession | None:
        return self._load_session_model(session_id)

    def update_tool_map(self, session_id: UUID, tool_map: Dict[str, Dict[str, str]]) -> None:
        runtime = self._runtime_sessions.get(session_id)
        if not runtime:
            # Do not fail hard; just log
            logger.warning("update_tool_map called for unknown runtime session %s", session_id)
            return
        runtime.tool_name_map = tool_map

    def get_tool_mapping(self, session_id: UUID) -> Dict[str, Dict[str, str]]:
        runtime = self._runtime_sessions.get(session_id)
        print(f'runtime is {runtime}')
        return runtime.tool_name_map if runtime else {}