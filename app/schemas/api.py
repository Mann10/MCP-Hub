from datetime import datetime
from typing import Any, Dict, List
from uuid import UUID

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    servers: List[str] = Field(..., description="List of provider names to include in this session")
    credentials: Dict[str, Dict[str, Any]] = Field(
        ..., description="Per-provider credentials; shape depends on auth_type"
    )


class CreateSessionResponse(BaseModel):
    session_id: UUID
    mcp_endpoint: str
    status: str = "created"


class SessionInfoResponse(BaseModel):
    id: UUID
    state: str
    servers: List[str]
    created_at: datetime
    updated_at: datetime