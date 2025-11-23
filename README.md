# Multi-MCP Gateway

A prototype **Multi-MCP Gateway** built with **FastAPI** and **Python**, using **SQLite** for session persistence.

The gateway exposes a **per-session MCP endpoint** that multiplexes multiple backend HTTP MCP servers.

## Features

- `POST /create-session` to create a new session with one or more MCP backends.
- `POST /session/{session_id}/mcp` generic MCP JSON-RPC endpoint:
  - `initialize`:
    - Forwards to all configured backends
    - Merges their tool lists into a flat list with **prefixed names**: `provider.tool_name`
  - `tools/call`:
    - Routes calls based on tool name prefix to the correct backend
- Supports backend auth types:
  - **Bearer** (`Authorization: Bearer <token>`)
  - **API key** (e.g. `x-api-key: <key>`)
- HTTP-only backends using `httpx.AsyncClient` (WebSocket-ready design).
- Persistent sessions using **SQLite** via `SQLModel`:
  - Sessions are reloaded on startup (best-effort).
- Simple retries with exponential backoff for backend calls.
- Basic unit tests using `pytest`, `pytest-asyncio`, and `respx`.

## Project Structure

```text
app/
  main.py                # FastAPI app and wiring
  config.py              # Settings/env loading
  registry.yaml          # Backend registry (providers)
  controllers/
    gateway_controller.py
  services/
    session_manager.py
    registry_loader.py
    auth_manager.py
    connection_manager.py
    multiplexer.py
    protocol_handler.py
  db/
    models.py
    database.py
  schemas/
    api.py
  utils/
    id_map.py
    retries.py
tests/
  test_create_session.py
  test_initialize_flow.py