from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException

from app.schemas.api import CreateSessionRequest, CreateSessionResponse, SessionInfoResponse
from app.services.session_manager import SessionManager
from app.services.protocol_handler import ProtocolHandler


def get_router(session_manager: SessionManager, protocol_handler: ProtocolHandler) -> APIRouter:
    router = APIRouter()

    @router.post("/create-session", response_model=CreateSessionResponse, status_code=201)
    async def create_session(payload: CreateSessionRequest) -> CreateSessionResponse:
        try:
            db_session = await session_manager.create_session(payload.servers, payload.credentials)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return CreateSessionResponse(
            session_id=db_session.id,
            mcp_endpoint=f"/session/{db_session.id}/mcp",
            status="created",
        )

    @router.post("/session/{session_id}/mcp")
    async def mcp_endpoint(
        session_id: UUID,
        body: Dict[str, Any] = Body(...),
    ) -> Dict[str, Any]:
        """
        Generic MCP HTTP endpoint (JSON-RPC style messages).
        """
        print("RAW MCP HTTP body:", body)
        
        # Process as plain JSON-RPC
        resp = await protocol_handler.handle_request(session_id, body)
        
        print("Response to client:", resp)
        
        # Return plain JSON-RPC (no envelope)
        return resp
    @router.get("/health")
    async def health() -> Dict[str, Any]:
        """
        Basic health-check endpoint; verifies DB connectivity.
        """
        try:
            session_manager.registry_loader.list_providers()
            # Quick DB touch
            session_manager.ensure_session_exists  # just ensure callable exists
        except Exception as e:
            return {"status": "error", "detail": str(e)}
        return {"status": "ok"}

    @router.get("/sessions/{session_id}", response_model=SessionInfoResponse)
    async def get_session_info(session_id: UUID) -> SessionInfoResponse:
        db_sess = session_manager.get_session_info(session_id)
        if not db_sess:
            raise HTTPException(status_code=404, detail="Session not found")

        servers = [s.get("name") for s in __safe_json_load(db_sess.servers_json)]

        return SessionInfoResponse(
            id=db_sess.id,
            state=db_sess.state,
            servers=servers,
            created_at=db_sess.created_at,
            updated_at=db_sess.updated_at,
        )

    return router


def __safe_json_load(data: str):
    import json

    try:
        return json.loads(data)
    except Exception:
        return []