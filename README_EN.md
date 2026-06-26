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
- **Multi-Account Routing** - Priority, weight, and weighted-load routing with automatic failover
- **Account Diagnostics** - Enable/disable accounts, set weight/priority, refresh tokens, and run single-account tests
- **Official Balance** - Accounts page can read official Work Buddy resource balance, credits expiring within 30 days, and package details
- **Balance Snapshots** - Use a local current-balance snapshot as fallback when the official balance API is unavailable
- **Manual Daily Credit Claim** - Claim today's credits per account or for all enabled accounts from the Accounts page
- **Token Auto-Refresh** - Automatically refreshes tokens before expiry
- **API Key Management** - Create separate keys for OpenCode, Cherry Studio, etc.
- **Secure Key Storage** - Only SHA-256 hashes stored; full key shown once at creation
- **Model Permission Control** - Restrict keys to specific models
- **Daily Request Limits** - Set per-key daily request caps
- **Dashboard** - Health, official credit summary, expiry reminders, request trend, model ranking, account status, key usage, and recent logs
- **Web Management UI** - Manage accounts, keys, models, logs, and settings in browser
- **Request Logging** - Records model, tokens, credit, duration, status codes, errors, with filtering, search, pagination, and details
- **Client Setup Wizard** - Settings page presets for OpenCode / OpenClaw, sub2api Docker, Cherry Studio, NextChat, and curl
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

For local browser access, the Web UI uses a same-origin HttpOnly admin cookie automatically. You usually do not need to paste the Admin Token manually.

For remote access or cookie fallback cases, set a fixed admin token:

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

On Windows Docker Desktop, use the helper script. It automatically finds the current Windows user's Work Buddy auth directory and mounts it into the container as read-only `/auth`:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1
```

If you control Docker from WSL:

```bash
chmod +x start-docker-wsl.sh
./start-docker-wsl.sh
```

Both scripts look for:

```text
C:\Users\<your username>\AppData\Local\CodeBuddyExtension\Data\Public\auth
```

Inside the container it appears as:

```text
/auth
```

So the Web UI "rescan / one-click import" flow can find accounts without manually pasting `.info` files.

If you only want to start the service without mounting the Windows auth directory:

```bash
docker-compose up -d
```

Open browser at:

```text
http://127.0.0.1:8787
```

Note: Docker containers cannot magically scan the Windows C drive. The auth directory must be mounted as a volume. The scripts automate that read-only mount.

## Usage Flow

1. Open Work Buddy / CodeBuddy and confirm you're logged in.
2. Start this project.
3. Check the "Accounts" page in Web UI to confirm accounts are imported.
4. Create a key in the "API Keys" page for your client.
5. Enter the Base URL and API Key in OpenCode, OpenClaw, Cherry Studio, NextChat, etc.

The Accounts page prefers the official Work Buddy resource balance. It shows the official remaining balance, credits expiring within 30 days, and each credit package's cycle, remaining amount, used amount, and expiry time. The daily claimed 150 credits appear as official resource packages; the actual expiry time follows the upstream response and is usually about one month.

Dashboard aggregates official balances across enabled accounts, credits expiring within 30 days, low-balance reminders, and stale-cache status. Official credit data uses a short local cache, so opening pages repeatedly does not spam the upstream API; manual refresh and successful claims force an update.

If the official balance API is temporarily unavailable, you can still enter the current remaining balance shown by Work Buddy as a local snapshot. This is only a fallback estimate: only new `usage.credit` after saving is deducted.

The Accounts page also provides manual daily credit claim actions. You can claim for one account or all enabled accounts. It does not run on a timer; if the upstream API reports already claimed, inactive campaign, or invalid account credentials, the UI shows that result directly.

## Client Setup

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Create in Web UI → API Keys |
| Model | `auto` / `glm-5.2` / `glm-5.1` / `kimi-k2.7` / `deepseek-v4-pro` / `deepseek-v4-flash` |
| Stream | Recommended |

Currently implemented OpenAI-compatible endpoints:

```text
/v1/chat/completions
/v1/models
```

If your client lets you choose the API type, select **OpenAI Compatible / Chat Completions**. Clients that are hard-wired to `/v1/responses` are not supported yet.

If the calling client runs inside Docker, `127.0.0.1` points to the container itself, not the Windows host. In that case the Base URL is usually:

```text
http://host.docker.internal:8787/v1
```

You can also copy common client presets directly from the Web UI "Settings" page.

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
| `--admin-token` | auto-generated | Admin API token; local Web UI usually uses Cookie auth automatically |
| `--no-admin-auth` | `false` | Disable admin auth, for local testing only |

## Environment Variables

| Variable | Description |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | Fixed admin token |
| `CB_GATEWAY_DB_PATH` | SQLite database path |
| `CB_AUTH_DIR` | Work Buddy / CodeBuddy auth file directory |
| `CB_HOST_AUTH_DIR` | Host auth directory used by Docker helper scripts |
| `CB_CONTAINER_AUTH_DIR` | Auth mount directory inside Docker, default `/auth` |

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
├── docker-compose.windows.yml
├── start.bat / start.sh
├── start-docker-win.ps1 / start-docker-wsl.sh
└── README.md
```

## License

MIT
