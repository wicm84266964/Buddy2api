# Work Buddy 2 API

> Expose a locally logged-in Tencent Work Buddy / CodeBuddy account as an OpenAI-compatible API for OpenCode, OpenClaw, Cherry Studio, NextChat, and other clients.

## What is this?

Work Buddy 2 API is a local gateway. It scans the auth files saved by the Work Buddy / CodeBuddy desktop app or extension, forwards requests to Tencent's model backend, and serves an OpenAI-compatible API on your machine.

In plain words: if you are already logged in to Work Buddy and have usable credits, this project exposes that access through `http://127.0.0.1:8787/v1` so other OpenAI-compatible clients can use it.

This project is intended for local personal use and testing. Do not expose it publicly, do not share it with other users, and do not share your auth files, API keys, database, or logs.

## Features

- **OpenAI-compatible API** - `/v1/chat/completions` and `/v1/models`
- **Streaming support** - SSE streaming and non-streaming aggregation
- **Account auto-import** - Scans local Work Buddy / CodeBuddy auth files on startup
- **Multi-account routing** - Least-used-first account selection and automatic failover
- **Token auto-refresh** - Refreshes expiring login tokens and writes them back to the database
- **API key management** - Create separate keys for OpenCode, Cherry Studio, or other clients
- **Hash-only key storage** - Full API keys are shown only once on creation
- **Model permissions** - Restrict keys to specific models
- **Daily request limits** - Optional per-key daily request count limits
- **Web admin UI** - Accounts, API Keys, Models, Logs, and Settings
- **Request logs** - Model, token usage, credit, duration, status code, and errors
- **Function calling passthrough** - Supports `tools` and `tool_calls`
- **Model aliases** - Built-in aliases plus custom aliases

## Prerequisites

1. Tencent Work Buddy / CodeBuddy is installed and logged in on this machine.
2. The logged-in account has usable model credits.
3. The gateway and client should preferably run on the same machine.

The default Windows auth scan path is similar to:

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

Use `CB_AUTH_DIR` if your auth files are stored elsewhere.

## Quick Start

### Windows

Double-click:

```bat
start.bat
```

Or run manually:

```powershell
pip install fastapi "uvicorn[standard]" httpx
python server.py
```

The server prints a temporary Admin Token on startup. Open the Web UI and set the token from the lower-left corner.

To use a fixed admin token:

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="change-this-token"
python server.py
```

### Linux / macOS

```bash
chmod +x start.sh
./start.sh
```

### Docker

```bash
docker-compose up -d
```

Open:

```text
http://127.0.0.1:8787
```

## Usage Flow

1. Open Work Buddy / CodeBuddy and make sure you are logged in.
2. Start this project.
3. Confirm the account is imported in the Web UI Accounts page.
4. Create an API key in the API Keys page.
5. Configure OpenCode, OpenClaw, Cherry Studio, NextChat, or another OpenAI-compatible client with the Base URL and API key.

## Client Setup

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Create in Web UI -> API Keys |
| Model | `auto` / `glm-5.2` / `glm-5.1` / `kimi-k2.7` / `deepseek-v4-pro` / `deepseek-v4-flash` |
| Stream | Recommended |

### OpenCode Example

Add an OpenAI-compatible provider:

```json
{
  "provider": {
    "workbuddy": {
      "name": "workbuddy",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-xxxxx"
      },
      "models": {
        "auto": {
          "name": "WorkBuddy Auto",
          "limit": {
            "context": 200000,
            "output": 32000
          }
        },
        "glm-5.2": {
          "name": "GLM-5.2",
          "limit": {
            "context": 200000,
            "output": 32000
          }
        }
      }
    }
  }
}
```

Run:

```powershell
opencode run -m workbuddy/auto "hello"
```

### curl Example

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-xxxxx" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Start Parameters

| Parameter | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8787` | Listen port |
| `--admin-token` | auto-generated | Admin API token |
| `--no-admin-auth` | `false` | Disable admin auth, only for temporary local testing |

## Environment Variables

| Variable | Description |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | Fixed admin token |
| `CB_GATEWAY_DB_PATH` | SQLite database path |
| `CB_AUTH_DIR` | Work Buddy / CodeBuddy auth file directory |

## Data and Security

- `codebuddy_gateway.db` stores imported account credentials and request logs.
- API keys are stored as hashes. Full keys are shown only once.
- Do not share the database, auth files, `.lab-agent`, logs, or screenshots.
- Do not expose the gateway to the public internet.
- For local use, keep the default `127.0.0.1` binding.

## File Structure

```text
buddy2api/
├── server.py           # FastAPI main service
├── proxy.py            # Request proxy
├── auth_manager.py     # Work Buddy / CodeBuddy auth management
├── database.py         # SQLite data layer
├── web/index.html      # Vue 3 Web UI
├── Dockerfile
├── docker-compose.yml
├── start.bat / start.sh
└── README.md
```

## License

MIT
