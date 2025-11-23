from __future__ import annotations
import asyncio
from typing import Dict
from uuid import UUID

import httpx

from app.config import settings
from app.utils.retries import async_retry

# Avoid circular import at runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.services.session_manager import RuntimeSessionState


class BackendHandle:
    """
    Lightweight wrapper around httpx.AsyncClient providing
    a `.post(json=...)` coroutine with retry + timeout.
    """

    def __init__(self, base_url: str, headers: Dict[str, str]) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers)
        self._closed = False

    async def post(self, json: dict, timeout: float | None = None) -> httpx.Response:
        if self._closed:
            raise RuntimeError("BackendHandle is closed")

        async def do_request() -> httpx.Response:
            return await self._client.post("", json=json, timeout=timeout or settings.backend_timeout)

        resp = await async_retry(
            do_request,
            retries=settings.retry_attempts,
            base_delay=settings.retry_backoff_base,
            exceptions=(httpx.RequestError,),
        )
        return resp
    def update_headers(self, headers: Dict[str, str]) -> None:
        """
        Update default headers for all future requests from this client.
        """
        self._client.headers.update(headers)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()


class ConnectionManager:
    """
    Factory/manager for httpx.AsyncClient instances scoped by (session_id, provider).
    """

    def __init__(self) -> None:
        # Map: session_id -> provider -> BackendHandle
        self._handles: Dict[UUID, Dict[str, BackendHandle]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_handle(
        self,
        session_id: UUID,
        provider_name: str,
        rpc_endpoint: str,
        headers: Dict[str, str],
        runtime: "RuntimeSessionState | None" = None,
    ) -> BackendHandle:
        async with self._lock:
            per_session = self._handles.setdefault(session_id, {})
            if provider_name in per_session:
                return per_session[provider_name]
            merged_headers: Dict[str, str] = dict(headers or {})
            if runtime is not None:
                provider_headers = runtime.provider_session_headers.get(provider_name, {})
                merged_headers.update(provider_headers)
            handle = BackendHandle(base_url=rpc_endpoint, headers=headers)
            per_session[provider_name] = handle
            return handle

    def get_handle(self, session_id: UUID, provider_name: str) -> BackendHandle | None:
        return self._handles.get(session_id, {}).get(provider_name)

    async def aclose_all(self) -> None:
        async with self._lock:
            for per_session in self._handles.values():
                for handle in per_session.values():
                    await handle.aclose()
            self._handles.clear()