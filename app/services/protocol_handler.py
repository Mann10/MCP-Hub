import logging
from typing import Any, Dict
from uuid import UUID

import httpx
import json

from app.config import settings
from app.utils.id_map import IdMapper
from .session_manager import SessionManager
from .multiplexer import MCPMultiplexer

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


class ProtocolHandler:
    """
    Parses MCP JSON-RPC messages and routes them appropriately.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        multiplexer: MCPMultiplexer,
        id_mapper: IdMapper,
    ) -> None:
        self.session_manager = session_manager
        self.multiplexer = multiplexer
        self.id_mapper = id_mapper

    async def handle_request(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a generic MCP JSON-RPC-like request.
        Required fields: jsonrpc, method, id
        """
        try:
            self.session_manager.ensure_session_exists(session_id)
        except KeyError:
            return self._jsonrpc_error(
                body,
                code=-32000,
                message=f"Unknown session {session_id}",
            )

        method = body.get("method")
        if not method:
            return self._jsonrpc_error(body, code=-32600, message="Missing 'method' in request")

        if method == "initialize":
            return await self._handle_initialize(session_id, body)
        elif method == "tools/list":
            return await self._handle_tools_list(session_id, body)
        elif method == "tools/call":
            return await self._handle_tools_call(session_id, body)
        else:
            return self._jsonrpc_error(
                body, code=-32601, message=f"Method '{method}' is not supported by gateway"
            )

    async def _handle_initialize(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
        combined_result = await self.multiplexer.initialize(session_id, body)
        print(f'combined result :- {combined_result}')
        return {
            "jsonrpc": body.get("jsonrpc", "2.0"),
            "id": body.get("id"),
            "result": combined_result,
        }
    
    async def _handle_tools_list(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        tools/list: similar merging logic to initialize, but only returns { tools: [...] }.
        """
        combined_result = await self.multiplexer.list_tools(session_id, body)
        return {
            "jsonrpc": body.get("jsonrpc", "2.0"),
            "id": body.get("id"),
            "result": combined_result,
        }
    
    async def _handle_tools_call(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Route tools/call to the correct backend based on prefixed tool name.
        Uses tool_name_map to resolve provider and backend tool name.
        """
        params = body.get("params") or {}
        tool_name = params.get("name")
        
        # ✅ LOG: Incoming request
        logger.info("tools/call received: tool=%s, session=%s", tool_name, session_id)
        
        if not tool_name:
            return self._jsonrpc_error(body, code=-32602, message="Missing tool 'name' in params")

        # Look up the tool in the session's tool_name_map
        tool_map = self.session_manager.get_tool_mapping(session_id)
        tool_info = tool_map.get(tool_name)
        
        if not tool_info:
            return self._jsonrpc_error(
                body,
                code=-32602,
                message=f"Unknown tool: {tool_name}. Tool may not exist or session may need reinitialization.",
            )

        provider = tool_info["provider"]
        backend_tool_name = tool_info["backend_tool_name"]
        
        # ✅ LOG: Tool mapping resolved
        logger.info("Resolved tool: %s -> provider=%s, backend_tool=%s", tool_name, provider, backend_tool_name)

        # Get the runtime state to access connections
        runtime = await self.session_manager.get_runtime_state(session_id)
        handle = runtime.connections.get(provider)
        
        if not handle:
            return self._jsonrpc_error(
                body,
                code=-32001,
                message=f"Provider '{provider}' not available in this session",
            )

        # Prepare forwarded request body with the backend tool name
        forward_body = dict(body)
        forward_params = dict(params)
        forward_params["name"] = backend_tool_name  # Use original backend tool name
        forward_body["params"] = forward_params

        # Map client ID to backend ID for response correlation
        client_id = body.get("id")
        backend_id = self.id_mapper.register(session_id, provider, client_id)
        forward_body["id"] = backend_id
        
        # ✅ LOG: Request being forwarded to backend
        logger.info("Forwarding to %s: method=%s, name=%s, id=%s->%s", 
                    provider, forward_body.get("method"), backend_tool_name, client_id, backend_id)

        # Forward request to backend
        try:
            resp = await handle.post(json=forward_body, timeout=settings.backend_timeout)
            resp.raise_for_status()
            #backend_payload = resp.json()
            body_text=resp.text
            # ✅ LOG: Raw backend response
            #logger.info("Backend %s responded: status=%s, payload_type=%s, payload=%s", 
                        #provider, resp.status_code, type(backend_payload).__name__, backend_payload)
            content_type = resp.headers.get("content-type", "")
            logger.info(
                "Backend %s HTTP response: status=%s, content-type=%s",
                provider, resp.status_code, content_type,
            )
            if content_type.startswith("text/event-stream"):
                try:
                    backend_payload = parse_sse_json_body(body_text)
                except Exception as e:
                    logger.warning("Failed to parse SSE body from provider=%s: %s", provider, e)
                    return self._jsonrpc_error(
                        body,
                        code=-32004,
                        message=f"Backend '{provider}' returned invalid SSE/JSON",
                    )
            else:
                try:
                    backend_payload = resp.json()
                except ValueError as e:
                    logger.warning("tools/call invalid JSON for provider=%s: %s", provider, e)
                    return self._jsonrpc_error(
                        body,
                        code=-32004,
                        message=f"Backend '{provider}' returned invalid JSON",
                    )
            # Validate that backend returned a dict
            if not isinstance(backend_payload, dict):
                logger.error("Backend returned non-dict response: %s", type(backend_payload))
                return self._jsonrpc_error(
                    body,
                    code=-32603,
                    message=f"Backend '{provider}' returned invalid response format",
                )
                
        except httpx.TimeoutException:
            logger.warning("tools/call timeout for provider=%s tool=%s", provider, backend_tool_name)
            return self._jsonrpc_error(
                body,
                code=-32002,
                message=f"Backend '{provider}' timeout during tools/call",
            )
        except httpx.HTTPStatusError as e:
            logger.warning("tools/call HTTP error for provider=%s: %s %s", provider, e.response.status_code, e.response.text)
            return self._jsonrpc_error(
                body,
                code=-32003,
                message=f"Backend '{provider}' returned HTTP {e.response.status_code}",
                data={"detail": e.response.text[:200]},
            )
        except httpx.RequestError as e:
            logger.warning("tools/call request error for provider=%s: %s", provider, e)
            return self._jsonrpc_error(
                body,
                code=-32003,
                message=f"Backend '{provider}' request failed",
                data={"detail": str(e)},
            )
        except ValueError as e:
            logger.warning("tools/call invalid JSON for provider=%s: %s", provider, e)
            return self._jsonrpc_error(
                body,
                code=-32004,
                message=f"Backend '{provider}' returned invalid JSON",
            )

        # Translate backend ID back to client ID
        backend_resp_id = backend_payload.get("id")
        orig_client_id = self.id_mapper.resolve_backend(session_id, provider, backend_resp_id) or client_id
        
        # ✅ LOG: ID translation
        logger.info("Translating response ID: backend=%s -> client=%s", backend_resp_id, orig_client_id)
        
        backend_payload["id"] = orig_client_id
        
        # Ensure jsonrpc field is present
        if "jsonrpc" not in backend_payload:
            backend_payload["jsonrpc"] = "2.0"

        # ✅ LOG: Final response being returned
        logger.info("Returning tools/call response: id=%s, has_result=%s, has_error=%s, jsonrpc=%s", 
                    backend_payload.get("id"), 
                    "result" in backend_payload,
                    "error" in backend_payload,
                    backend_payload.get("jsonrpc"))
        logger.debug("Full response payload: %s", backend_payload)

        return backend_payload
    
    # async def _handle_tools_call(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
    #     """
    #     Route tools/call to the correct backend based on the prefixed tool name.

    #     - External name (from client):  e.g. "huggingface__model_search"
    #     - Internal mapping (tool_name_map): {"provider": "huggingface", "backend_tool_name": "model_search"}
    #     - Name forwarded to backend:   e.g. "model_search"
    #     """
    #     try:
    #         params = body.get("params") or {}
    #         name = params.get("name")

    #         if not name:
    #             return self._jsonrpc_error(body, code=-32602, message="Missing tool 'name' in params")

    #         # --- 1) Get runtime + tool mapping for this session ---
    #         runtime = await self.session_manager.get_runtime_state(session_id)
    #         tool_map = self.session_manager.get_tool_mapping(session_id) or {}

    #         # First try the explicit mapping from multiplexer.initialize/list
    #         mapping = tool_map.get(name)
    #         provider: str | None = None
    #         backend_tool_name: str | None = None

    #         if mapping:
    #             provider = mapping.get("provider")
    #             backend_tool_name = mapping.get("backend_tool_name")

    #         # --- 2) Fallback: parse provider/tool from the name if not in map ---
    #         # Supports both "provider__tool" and "provider.tool"
    #         if not provider or not backend_tool_name:
    #             if "__" in name:
    #                 provider, backend_tool_name = name.split("__", 1)
    #             elif "." in name:
    #                 provider, backend_tool_name = name.split(".", 1)
    #             else:
    #                 return self._jsonrpc_error(
    #                     body,
    #                     code=-32602,
    #                     message=(
    #                         f"Tool name '{name}' must be prefixed with provider "
    #                         "(e.g. 'provider__tool' or 'provider.tool')"
    #                     ),
    #                 )

    #         # --- 3) Check there is a connection for this provider in this session ---
    #         handle = runtime.connections.get(provider)
    #         if not handle:
    #             return self._jsonrpc_error(
    #                 body,
    #                 code=-32001,
    #                 message=f"Provider '{provider}' not available in this session",
    #             )

    #         # --- 4) Build forwarded request body, using the BACKEND tool name ---
    #         forward_body = dict(body)
    #         forward_params = dict(params)
    #         forward_params["name"] = backend_tool_name   # <-- key: backend sees only its own tool name
    #         forward_body["params"] = forward_params

    #         client_id = body.get("id")
    #         backend_id = self.id_mapper.register(session_id, provider, client_id)
    #         forward_body["id"] = backend_id

    #         logger.info(
    #             "Forwarding tools/call: session=%s, provider=%s, public_name=%s, backend_name=%s",
    #             session_id,
    #             provider,
    #             name,
    #             backend_tool_name,
    #         )

    #         # --- 5) Send to backend ---
    #         try:
    #             resp = await handle.post(json=forward_body, timeout=settings.backend_timeout)
    #             resp.raise_for_status()
    #             backend_payload = resp.json()
    #         except httpx.TimeoutException:
    #             logger.warning("tools/call timeout for provider=%s", provider)
    #             return self._jsonrpc_error(
    #                 body,
    #                 code=-32002,
    #                 message=f"Backend '{provider}' timeout during tools/call",
    #             )
    #         except httpx.HTTPError as e:
    #             logger.warning("tools/call HTTP error for provider=%s: %s", provider, e)
    #             return self._jsonrpc_error(
    #                 body,
    #                 code=-32003,
    #                 message=f"Backend '{provider}' HTTP error",
    #                 data={"detail": str(e)},
    #             )
    #         except ValueError as e:
    #             logger.warning("tools/call invalid JSON for provider=%s: %s", provider, e)
    #             return self._jsonrpc_error(
    #                 body,
    #                 code=-32004,
    #                 message=f"Backend '{provider}' returned invalid JSON",
    #             )

    #         # --- 6) Translate backend id back to client id, normalize jsonrpc ---
    #         backend_resp_id = backend_payload.get("id")
    #         orig_client_id = self.id_mapper.resolve_backend(session_id, provider, backend_resp_id) or client_id
    #         backend_payload["id"] = orig_client_id
    #         backend_payload["jsonrpc"] = backend_payload.get("jsonrpc", body.get("jsonrpc", "2.0"))

    #         return backend_payload

    #     except Exception as e:
    #         # Catch ANY unexpected error and turn it into a proper JSON-RPC error
    #         logger.exception("Unhandled error in tools/call for session=%s: %s", session_id, e)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32099,
    #             message="Internal error in gateway during tools/call",
    #             data={"detail": str(e)},
    #         )
    
    
    # async def _handle_tools_call(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
    #     """
    #     Route tools/call to the correct backend based on prefixed tool name.
    #     Uses tool_name_map to resolve provider and backend tool name.
    #     """
    #     params = body.get("params") or {}
    #     tool_name = params.get("name")
        
    #     if not tool_name:
    #         return self._jsonrpc_error(body, code=-32602, message="Missing tool 'name' in params")

    #     # Look up the tool in the session's tool_name_map
    #     tool_map = self.session_manager.get_tool_mapping(session_id)
    #     tool_info = tool_map.get(tool_name)
        
    #     if not tool_info:
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32602,
    #             message=f"Unknown tool: {tool_name}. Tool may not exist or session may need reinitialization.",
    #         )

    #     provider = tool_info["provider"]
    #     backend_tool_name = tool_info["backend_tool_name"]

    #     # Get the runtime state to access connections
    #     runtime = await self.session_manager.get_runtime_state(session_id)
    #     handle = runtime.connections.get(provider)
        
    #     if not handle:
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32001,
    #             message=f"Provider '{provider}' not available in this session",
    #         )

    #     # Prepare forwarded request body with the backend tool name
    #     forward_body = dict(body)
    #     forward_params = dict(params)
    #     forward_params["name"] = backend_tool_name  # Use original backend tool name
    #     forward_body["params"] = forward_params

    #     # Map client ID to backend ID for response correlation
    #     client_id = body.get("id")
    #     backend_id = self.id_mapper.register(session_id, provider, client_id)
    #     forward_body["id"] = backend_id

    #     # Forward request to backend
    #     try:
    #         resp = await handle.post(json=forward_body, timeout=settings.backend_timeout)
    #         resp.raise_for_status()
    #         backend_payload = resp.json()
    #     except httpx.TimeoutException:
    #         logger.warning("tools/call timeout for provider=%s tool=%s", provider, backend_tool_name)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32002,
    #             message=f"Backend '{provider}' timeout during tools/call",
    #         )
    #     except httpx.HTTPStatusError as e:
    #         logger.warning("tools/call HTTP error for provider=%s: %s %s", provider, e.response.status_code, e.response.text)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32003,
    #             message=f"Backend '{provider}' returned HTTP {e.response.status_code}",
    #             data={"detail": e.response.text[:200]},
    #         )
    #     except httpx.RequestError as e:
    #         logger.warning("tools/call request error for provider=%s: %s", provider, e)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32003,
    #             message=f"Backend '{provider}' request failed",
    #             data={"detail": str(e)},
    #         )
    #     except ValueError as e:
    #         logger.warning("tools/call invalid JSON for provider=%s: %s", provider, e)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32004,
    #             message=f"Backend '{provider}' returned invalid JSON",
    #         )

    #     # Translate backend ID back to client ID
    #     backend_resp_id = backend_payload.get("id")
    #     orig_client_id = self.id_mapper.resolve_backend(session_id, provider, backend_resp_id) or client_id
    #     backend_payload["id"] = orig_client_id
    #     backend_payload["jsonrpc"] = backend_payload.get("jsonrpc", body.get("jsonrpc", "2.0"))

    #     return backend_payload

    # async def _handle_tools_call(self, session_id: UUID, body: Dict[str, Any]) -> Dict[str, Any]:
    #     params = body.get("params") or {}
    #     name = params.get("name")
    #     #arguments = params.get("arguments", {})
        
    #     if not name:
    #         return self._jsonrpc_error(body, code=-32602, message="Missing tool 'name' in params")

    #     # if "." not in name:
    #     #     return self._jsonrpc_error(
    #     #         body,
    #     #         code=-32602,
    #     #         message=f"Tool name '{name}' must be prefixed with provider (e.g. 'provider.tool')",
    #     #     )
    #     print(name)
    #     provider, backend_tool_name = name.split("__", 1)

    #     # Make sure provider is known for this session
    #     runtime = await self.session_manager.get_runtime_state(session_id)
    #     handle = runtime.connections.get(provider)
    #     if not handle:
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32001,
    #             message=f"Provider '{provider}' not available in this session",
    #         )

    #     # Use tool map if we have it (in case backend tool name differs)
    #     tool_map = self.session_manager.get_tool_mapping(session_id)
    #     mapping = tool_map.get(name)
    #     if mapping:
    #         backend_tool_name = mapping["backend_tool_name"]

    #     # Prepare forwarded request body
    #     forward_body = dict(body)
    #     forward_params = dict(params)
    #     forward_params["name"] = backend_tool_name
    #     forward_body["params"] = forward_params

    #     client_id = body.get("id")
    #     backend_id = self.id_mapper.register(session_id, provider, client_id)
    #     forward_body["id"] = backend_id

    #     try:
    #         resp = await handle.post(json=forward_body, timeout=settings.backend_timeout)
    #         resp.raise_for_status()
    #         backend_payload = resp.json()
    #     except httpx.TimeoutException:
    #         logger.warning("tools/call timeout for provider=%s", provider)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32002,
    #             message=f"Backend '{provider}' timeout during tools/call",
    #         )
    #     except httpx.HTTPError as e:
    #         logger.warning("tools/call HTTP error for provider=%s: %s", provider, e)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32003,
    #             message=f"Backend '{provider}' HTTP error",
    #             data={"detail": str(e)},
    #         )
    #     except ValueError as e:
    #         logger.warning("tools/call invalid JSON for provider=%s: %s", provider, e)
    #         return self._jsonrpc_error(
    #             body,
    #             code=-32004,
    #             message=f"Backend '{provider}' returned invalid JSON",
    #         )

    #     # Translate backend id back to client id
    #     backend_resp_id = backend_payload.get("id")
    #     orig_client_id = self.id_mapper.resolve_backend(session_id, provider, backend_resp_id) or client_id
    #     backend_payload["id"] = orig_client_id
    #     backend_payload["jsonrpc"] = backend_payload.get("jsonrpc", body.get("jsonrpc", "2.0"))

    #     return backend_payload

    @staticmethod
    def _jsonrpc_error(
        request_body: Dict[str, Any],
        *,
        code: int,
        message: str,
        data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return {
            "jsonrpc": request_body.get("jsonrpc", "2.0"),
            "id": request_body.get("id"),
            "error": {
                "code": code,
                "message": message,
                **({"data": data} if data is not None else {}),
            },
        }