import asyncio
import logging
from typing import Any, Dict, List
from uuid import UUID
import re
import time
import json

import httpx

from .session_manager import SessionManager

logger = logging.getLogger(__name__)

def parse_sse_json_body(body: str):
    """
    Parse a single-event SSE body of the form:

        event: message
        data: {...json...}

    and return the JSON object.
    """
    data_line = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            # Everything after 'data:' is JSON
            data_line = line[len("data:"):].strip()
            break

    if data_line is None:
        raise ValueError(f"No 'data:' line found in SSE body: {body!r}")

    return json.loads(data_line)

class MCPMultiplexer:
    """
    Handles multi-backend MCP initialize, merging tool lists and
    recording prefixed tool-name mappings.
    """

    def __init__(self, session_manager: SessionManager) -> None:
        self.session_manager = session_manager
    
    @staticmethod
    def _make_prefixed_tool_name(provider: str, name: str) -> str:
        # Ensure both parts only contain allowed chars: [a-zA-Z0-9_-]
        safe_provider = re.sub(r"[^a-zA-Z0-9_-]", "_", provider)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        return f"{safe_provider}__{safe_name}"

    async def initialize(self, session_id: UUID, initialize_request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Forward initialize to all backends for this session, collect results,
        merge tools with prefixed names, and return a combined result object.
        """
        runtime = await self.session_manager.get_runtime_state(session_id)
        connections = runtime.connections
        async def call_provider(provider: str):
            handle = connections[provider]
            resp: httpx.Response | None = None
            try:
                resp = await handle.post(json=initialize_request,timeout=60)
                print(f'Response for {provider}: status={resp.status_code}')
                print(f'Headers for {provider}: {resp.headers}')
                body = resp.text
                print(f'Body repr for {provider}: {repr(body)}')
                if resp.headers.get("content-type", "").startswith("text/event-stream"):
                    data = parse_sse_json_body(body)
                else:
                    data = resp.json()

                print(f'Parsed JSON for {provider}: {data}')

                resp.raise_for_status()
                return provider, data, None, resp
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Initialize failed for provider %s: %s", provider, e)
                # still return 4 values even on error
                return provider, None, e, resp
        print(f'ConnectionKey are : {connections.keys()}')
        tasks = [call_provider(p) for p in connections.keys()]
        results = await asyncio.gather(*tasks)

        combined_tools: List[Dict[str, Any]] = []
        tool_map: Dict[str, Dict[str, str]] = {}
        server_info: List[Dict[str, Any]] = []
        base_result: Dict[str, Any] = {}

        for provider, payload, error, resp in results:
            if error or not payload:
                server_info.append(
                    {
                        "provider": provider,
                        "status": "error",
                        "message": str(error),
                    }
                )
                continue

            # --- persist_response_headers handling ---
            provider_cfg = self.session_manager.registry_loader.get_provider_config(provider)
            wanted = {h.lower() for h in getattr(provider_cfg, "persist_response_headers", [])}

            if resp is not None and wanted:
                session_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() in wanted
                }
                if session_headers:
                    # Save in runtime so future clients for this provider get them
                    runtime.provider_session_headers.setdefault(provider, {}).update(session_headers)

                    # And update the existing httpx client used for this provider
                    handle = connections[provider]
                    # If you added BackendHandle.update_headers()
                    handle.update_headers(session_headers)
                    # If instead you exposed .client property, do:
                    # handle.client.headers.update(session_headers)

                    logger.info(
                        "Persisted response headers for provider %s: %s",
                        provider,
                        session_headers,
                    )
            # -----------------------------------------

            # Use first successful result as the base_result template
            if not base_result and "result" in payload and isinstance(payload["result"], dict):
                base_result = dict(payload["result"])

            tools = (payload.get("result") or {}).get("tools") or []
            tool_count = 0
            for tool in tools:
                name = tool.get("name")
                if not name:
                    continue

                prefixed_name = self._make_prefixed_tool_name(provider, name)
                new_tool = dict(tool)
                new_tool["name"] = prefixed_name
                combined_tools.append(new_tool)

                tool_map[prefixed_name] = {
                    "provider": provider,
                    "backend_tool_name": name,
                }
                tool_count += 1

            server_info.append(
                {
                    "provider": provider,
                    "status": "ok",
                    "tool_count": tool_count,
                }
            )

        # Ensure result object exists
        if not base_result:
            base_result = {}
        base_result["tools"] = combined_tools
        base_result["server_info"] = server_info

        # Store mapping for tools/call routing
        self.session_manager.update_tool_map(session_id, tool_map)

        return base_result

    async def list_tools(self, session_id: UUID, request_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle tools/list across all backends.

        Forward the client's tools/list request to each backend, collect their
        tool lists, prefix tool names with provider, and return:

        {
          "tools": [ ... ],
          "server_info": [ ... ]
        }
        """
        runtime = await self.session_manager.get_runtime_state(session_id)
        connections = runtime.connections
        current_providers=frozenset(connections.keys())
        CACHE_TTL_SECONDS = 600  # 10 minutes; adjust as needed
        now = time.time()
        if (
            runtime.cached_tools is not None
            and runtime.cached_tools_ts is not None
            and runtime.cached_tools_providers == current_providers
            and (now - runtime.cached_tools_ts) < CACHE_TTL_SECONDS
        ):
            logger.info(
            "Returning cached tools for session %s (providers=%s)",
            session_id,
            list(current_providers),
        )

            cached = runtime.cached_tools
            result = cached["result"]
            tool_map = cached.get("tool_map") or {}

            # This should be very cheap: just set the map for this session
            self.session_manager.update_tool_map(session_id, tool_map)

            return result

        async def call_provider(provider: str):
            handle = connections[provider]
            try:
                resp = await handle.post(json=request_body,timeout=60)
                print(f'Response for {provider}: status={resp.status_code}')
                print(f'Headers for {provider}: {resp.headers}')
                body = resp.text
                print(f'Body repr for {provider}: {repr(body)}')
                if resp.headers.get("content-type", "").startswith("text/event-stream"):
                    data = parse_sse_json_body(body)
                else:
                    data = resp.json()
                return provider, data, None
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("tools/list failed for provider %s: %s", provider, e)
                return provider, None, e

        tasks = [call_provider(p) for p in connections.keys()]
        results = await asyncio.gather(*tasks)

        combined_tools: List[Dict[str, Any]] = []
        tool_map: Dict[str, Dict[str, str]] = {}
        server_info: List[Dict[str, Any]] = []

        for provider, payload, error in results:
            if error or not payload:
                server_info.append(
                    {
                        "provider": provider,
                        "status": "error",
                        "message": str(error),
                    }
                )
                continue

            tools = (payload.get("result") or {}).get("tools") or []
            tool_count = 0
            for tool in tools:
                name = tool.get("name")
                if not name:
                    continue

                prefixed_name = self._make_prefixed_tool_name(provider, name)
                new_tool = dict(tool)
                new_tool["name"] = prefixed_name
                combined_tools.append(new_tool)

                tool_map[prefixed_name] = {
                    "provider": provider,
                    "backend_tool_name": name,
                }
                tool_count += 1

            server_info.append(
                {
                    "provider": provider,
                    "status": "ok",
                    "tool_count": tool_count,
                }
            )

        # Update mapping so tools/call keeps working even if tools/list adds/removes tools
        self.session_manager.update_tool_map(session_id, tool_map)
        result = {
            "tools": combined_tools,
            "server_info": server_info,
        }
        # ----- SAVE CACHE -----
        runtime.cached_tools = {
            "result": result,
            "tool_map": tool_map
        }
        runtime.cached_tools_ts = time.time()
        runtime.cached_tools_providers = current_providers
        logger.info(
            "Cached tools for session %s (providers=%s)",
            session_id,
            list(current_providers),
        )
        # ----------------------

        return result