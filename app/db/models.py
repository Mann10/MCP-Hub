from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field


class MCPGatewaySession(SQLModel, table=True):
    """
    Persistent session record.

    TODO: In production, encrypt/seal credentials_json.
    """

    __tablename__ = "sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    state: str = Field(default="ready", index=True)  # initial, initializing, ready, failed

    # JSON strings
    servers_json: str = Field(description="JSON string of selected servers")
    credentials_json: str = Field(description="JSON string of credentials")

    # Reserved for future use (e.g., persisted tool map or metadata)
    #metadata_json: Optional[str] = None