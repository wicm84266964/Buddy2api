# Buddy2api

[English](README_EN.md) | 中文

> Convert your local Tencent Work Buddy / CodeBuddy accounts into an OpenAI-compatible API for use in OpenCode, OpenClaw, Cherry Studio, NextChat, and other tools.

## What is this?

Buddy2api is a local gateway. It scans the login credentials saved by the Work Buddy / CodeBuddy desktop app or extension on your machine, forwards requests to Tencent's model API, and exposes a standard OpenAI-compatible interface locally.

In short: you're already logged into Work Buddy with available credits. This project exposes those credits through `http://127.0.0.1:8787/v1` so other OpenAI-compatible clients can use them.

This project is mainly for personal use and testing. Do not deploy publicly, do not share with others, and do not send your login credentials, API keys, or database files to anyone.

## Features

- **OpenAI Compatible** - `/v1/chat/completions` and `/v1/models`
- **Streaming Output** - SSE streaming and non-streaming aggregated responses
- **Auto Import Accounts** - Scans Work Buddy / CodeBuddy auth files on startup
- **Multi-Account Load Balancing** - Least-used-first routing with automatic failover
- **Token Auto-Refresh** - Automatically refreshes tokens before expiry
- **API Key Management** - Create separate keys for OpenCode, Cherry Studio, etc.
- **Secure Key Storage** - Only SHA-256 hashes stored; full key shown once at creation
- **Model Permission Control** - Restrict keys to specific models
- **Daily Request Limits** - Set per-key daily request caps
- **Web Management UI** - Manage accounts, keys, models, logs, and settings in browser
- **Request Logging** - Records model, tokens, credit, duration, status codes, errors
- **Function Calling Passthrough** - Native `tools` / `tool_calls` support
- **Model Aliases** - Built-in common aliases with custom extension support

## Prerequisites

1. Work Buddy / CodeBuddy installed and logged in on this machine.
2. Logged-in account has available model credits.
3. Best to run this project and calling clients on the same machine.

Default Windows scan path:

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

Use `CB_AUTH_DIR` if your auth files are in a different directory.

## Quick Start

### Windows

Double-click:

```bat
start.bat
```

Or manually:

```powershell
pip install fastapi "uvicorn[standard]" httpx
python server.py
```

The console will print an Admin Token on startup. Open Web UI and click "Set Token" in the bottom-left to enter it.

Recommended to set a fixed token:

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

Open browser at:

```text
http://127.0.0.1:8787
```

## Usage Flow

1. Open Work Buddy / CodeBuddy and confirm you're logged in.
2. Start this project.
3. Check the "Accounts" page in Web UI to confirm accounts are imported.
4. Create a key in the "API Keys" page for your client.
5. Enter the Base URL and API Key in OpenCode, OpenClaw, Cherry Studio, NextChat, etc.

## Client Setup

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Create in Web UI → API Keys |
| Model | `auto` / `glm-5.2` / `glm-5.1` / `kimi-k2.7` / `deepseek-v4-pro` / `deepseek-v4-flash` |
| Stream | Recommended |

### OpenCode Example

Add an OpenAI-compatible provider in OpenCode:

```json
{
  "provider": {
    "buddy2api": {
      "name": "buddy2api",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-xxxxx"
      },
      "models": {
        "auto": {
          "name": "Buddy2api Auto",
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

Usage:

```powershell
opencode run -m buddy2api/auto "hello"
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
| `--no-admin-auth` | `false` | Disable admin auth, for local testing only |

## Environment Variables

| Variable | Description |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | Fixed admin token |
| `CB_GATEWAY_DB_PATH` | SQLite database path |
| `CB_AUTH_DIR` | Work Buddy / CodeBuddy auth file directory |

## Data and Security

- `codebuddy_gateway.db` stores imported account credentials and request logs.
- API keys are stored as hashes only; full key is never shown again after creation.
- Do not share your database, auth files, `.lab-agent`, logs, or screenshots.
- Do not expose the service to public network addresses.
- Keep the default `127.0.0.1` for safest local-only usage.

## File Structure

```text
buddy2api/
├── server.py           # FastAPI main service
├── proxy.py            # Request proxy
├── auth_manager.py     # Work Buddy / CodeBuddy credential management
├── database.py         # SQLite data layer
├── web/index.html      # Vue 3 Web UI
├── Dockerfile
├── docker-compose.yml
├── start.bat / start.sh
└── README.md
```

## License

MIT
