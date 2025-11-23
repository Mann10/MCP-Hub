from collections import defaultdict
from typing import Any, Dict, Optional
from uuid import uuid4, UUID


class IdMapper:
    """
    Tracks mapping between client request IDs and backend request IDs
    per (session, provider).

    This is future-proof for WebSocket / streaming where requests and
    responses can interleave. For HTTP, it's slightly overkill but keeps
    the design clean.
    """

    def __init__(self) -> None:
        # key: "session:provider" -> {backend_id_str: original_client_id}
        # We store backend_id as string (UUID), but client_id as-is (preserve type)
        self._backend_to_client: Dict[str, Dict[str, Any]] = defaultdict(dict)

    @staticmethod
    def _key(session_id: UUID, provider: str) -> str:
        return f"{session_id}:{provider}"

    def register(self, session_id: UUID, provider: str, client_id: Any) -> str:
        """
        Register a new outgoing request for mapping. Returns backend_id.
        Stores the original client_id with its type preserved.
        """
        backend_id = str(uuid4())
        key = self._key(session_id, provider)
        
        # ✅ Store original client_id (int, str, or None)
        self._backend_to_client[key][backend_id] = client_id
        
        return backend_id

    def resolve_backend(self, session_id: UUID, provider: str, backend_id: Any) -> Optional[Any]:
        """
        Resolve backend_id back to the original client_id.
        Returns the client_id with its original type preserved.
        """
        key = self._key(session_id, provider)
        # ✅ Return original client_id (preserves type)
        return self._backend_to_client.get(key, {}).get(str(backend_id))

    def clear_session(self, session_id: UUID) -> None:
        prefix = f"{session_id}:"
        to_delete = [k for k in self._backend_to_client if k.startswith(prefix)]
        for k in to_delete:
            self._backend_to_client.pop(k, None)