# Buddy 2 API

> Convert your desktop AI coding assistant's subscription quota into a standard OpenAI-compatible API, with a web management interface.

## What is this?

If you're using Tencent's "Claw" AI coding extension, you've probably noticed its built-in model credits are locked inside the extension. Buddy 2 API "frees" those credits into a standard OpenAI API, so you can use them in OpenClaw, Cherry Studio, NextChat, or any OpenAI-compatible client.

## Features

- **OpenAI Compatible** - `/v1/chat/completions` (streaming SSE + non-streaming), `/v1/models`
- **Multi-Account Load Balancing** - Auto-scan, least-used-first routing, automatic failover
- **Token Auto-Refresh** - 60s early expiry detection, automatic refresh and writeback
- **API Key Management** - SHA-256 hash storage, model permissions, daily request limits
- **Web Management UI** - Dashboard, Accounts, Keys, Models, Logs, Settings
- **Function Calling** - Native `tools` / `tool_calls` passthrough
- **Request Logging** - Token consumption, credit tracking, duration, status codes
- **Model Aliases** - Built-in aliases (gpt-4o → glm-5.2, claude-3.5-sonnet → deepseek-v4-pro, etc.) with custom alias support

## Quick Start

### Windows

Double-click `start.bat`

Or manually:

```powershell
pip install fastapi "uvicorn[standard]" httpx
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

Open browser at `http://127.0.0.1:8787`

## Prerequisites

1. Desktop AI coding assistant installed (VS Code extension form)
2. Extension logged in with available model credits

On startup, auth files are automatically scanned and accounts are imported.

## Client Setup

### OpenClaw / Cherry Studio / NextChat etc.

| Field | Value |
|-------|-------|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Create in Web UI → API Keys |
| Model | `auto` / `glm-5.2` / `kimi-k2.7` / `deepseek-v4-pro` |
| Stream | Recommended |

Model alias mapping supported: `gpt-4o` → `glm-5.2`, `claude-3.5-sonnet` → `deepseek-v4-pro`, etc.

### curl

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-xxxxx" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Start Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8787` | Listen port |
| `--admin-token` | auto-generated | Admin API token |
| `--no-admin-auth` | `false` | Disable admin auth |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CB_GATEWAY_ADMIN_TOKEN` | Fixed admin token |
| `CB_GATEWAY_DB_PATH` | SQLite database path |
| `CB_AUTH_DIR` | Auth file directory |

## File Structure

```
buddy2api/
├── server.py           # FastAPI main service
├── proxy.py            # Request proxy
├── auth_manager.py     # Multi-account management
├── database.py         # SQLite data layer
├── web/index.html      # Vue 3 Web UI
├── Dockerfile
├── docker-compose.yml
├── start.bat / start.sh
└── README.md
```

## License

MIT
