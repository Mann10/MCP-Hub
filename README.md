# Multi-MCP Gateway

A prototype **Multi-MCP Gateway** built with **FastAPI** and **Python**, using **SQLite** to save sessions.  
The gateway exposes a **per-session MCP endpoint** that connects to multiple backend HTTP MCP servers.

---

## Features

- `POST /create-session`  
  Create a new session with one or more MCP backends.

- `POST /session/{session_id}/mcp`  
  Generic MCP JSON-RPC endpoint:
  - `initialize`:
    - Sends the request to all configured backends.
    - Merges their tool lists into a flat list with **prefixed names**: `provider.tool_name`.
  - `tools/call`:
    - Routes calls based on the tool name prefix to the correct backend.

- Supports backend auth types:
  - **Bearer** (`Authorization: Bearer <token>`)
  - **API key** (for example, `x-api-key: <key>`)

- HTTP backends using `httpx.AsyncClient` (WebSocket-ready design).

- Persistent sessions using **SQLite** via SQLModel:
  - Sessions are reloaded on startup (best effort).

- Simple retries with exponential backoff for backend calls.

- Basic unit tests using `pytest`, `pytest-asyncio`, and `respx`.

---

## Project Structure
```text
app/
├── main.py                # FastAPI app and wiring
├── config.py              # Settings/env loading
├── registry.yaml          # Backend registry (providers)
├── controllers/
│   └── gateway_controller.py
├── services/
│   ├── session_manager.py
│   ├── registry_loader.py
│   ├── auth_manager.py
│   ├── connection_manager.py
│   ├── multiplexer.py
│   └── protocol_handler.py
├── db/
│   ├── models.py
│   └── database.py
├── schemas/
│   └── api.py
└── utils/
├── id_map.py
└── retries.py
tests/
├── test_create_session.py
└── test_initialize_flow.py
```
---
## Setup

### 1. Clone the Repository

Go to the folder where you want the project (for example, Desktop).

```text
1. git clone <repo-url>

2. Navigate to Project Folder
 cd <project-folder>

3. Create Virtual Environment
 You can use pipenv, uv, or plain venv. For example:
 python -m venv .venv

4. Activate Virtual Environment
 Command depends on your OS and shell.

5. Start FastAPI
 uvicorn app.main:app --reload
 The app will run on your localhost (for example http://127.0.0.1:8000).
 Open http://127.0.0.1:8000/docs in your browser to see the FastAPI Swagger UI.

6. Creating a Session
  Use this curl command to create a session:
    curl -X 'POST' \
      'http://127.0.0.1:8000/create-session' \
      -H 'accept: application/json' \
      -H 'Content-Type: application/json' \
      -d '{
      "servers": ["huggingface", "aws_knowledge_base", "langgraphmcp"],
      "credentials": {
        "huggingface": { "token": "YourHuggingFaceToken" },
        "aws_knowledge_base": {},
        "langgraphmcp": {}
      }
    }'
The response will look like this:
    {
      "session_id": "02fd5173-78aa-4a99-abdb-81445694ee5c",
      "mcp_endpoint": "/session/02fd5173-78aa-4a99-abdb-81445694ee5c/mcp",
      "status": "created"
    }

Take the mcp_endpoint from the response and attach your localhost URL in front of it.
For example:
http://127.0.0.1:8000/session/02fd5173-78aa-4a99-abdb-81445694ee5c/mcp

As of now, the project includes these MCP backends:

* huggingface
* aws_knowledge_base
* langgraphmcp

You can check the registry.yaml file and add other HTTP/HTTPS MCP servers there.
Right now this system works with HTTP/HTTPS servers only.
You can query all three MCP backends as much as you like and add more by following the same rules used in registry.yaml.

Using with Claude
1. Open Claude Config File
There is an image in the repo that shows where it is.
If you cannot find it, search online for "how to find Claude config file".
2. Remove Existing Config
Remove everything inside the mcpServers section.
3. Add Gateway Config
Add this content (update the URL with your own mcp_endpoint):
jsonDownloadCopy code{
  "mcpServers": {
    "xyz": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/session/02fd5173-78aa-4a99-abdb-81445694ee5c/mcp"
      ]
    }
  }
}
4. Test the Setup
After saving the config, you can ask Claude something like:

I want to set up Lambda in my AWS account. How do I do it?

This will use the aws_knowledge_base MCP server behind the gateway.
You can also see the request logs in the terminal where your FastAPI app is running.
5. Remove Config (Optional)
If you no longer need this setup, simply delete this MCP config entry in the Claude config file (there is a delete icon next to it in the Claude UI).
