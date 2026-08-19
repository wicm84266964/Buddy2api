# Buddy2api

[English](README_EN.md) | 中文

> Convert your local Tencent Work Buddy / CodeBuddy accounts into an OpenAI-compatible API for use in OpenCode, OpenClaw, Cherry Studio, NextChat, and other tools.

## What is this?

Buddy2api is a local gateway. It scans the login credentials saved by the Work Buddy / CodeBuddy desktop app or extension on your machine, forwards requests to Tencent's model API, and exposes a standard OpenAI-compatible interface locally.

In short: you're already logged into Work Buddy with available credits. This project exposes those credits through `http://127.0.0.1:8787/v1` so other OpenAI-compatible clients can use them.

This project is mainly for personal use and testing. Do not deploy publicly, do not share with others, and do not send your login credentials, API keys, or database files to anyone.

## Features

- **OpenAI Compatible** - `/v1/chat/completions`, `/v1/responses`, and `/v1/models`
- **Streaming Output** - SSE streaming and non-streaming aggregated responses
- **Auto Import Accounts** - Scans Work Buddy / CodeBuddy auth files on startup
- **Multi-Account Routing** - Higher priority accounts are used first; accounts at the same priority stay sticky when possible, with automatic failover
- **Account Diagnostics** - Enable/disable accounts, set weight/priority, refresh tokens, and run single-account tests
- **Official Balance** - Accounts page can read official Work Buddy resource balance, credits expiring within 30 days, and package details
- **Balance Snapshots** - Use a local current-balance snapshot as fallback when the official balance API is unavailable
- **Manual Daily Credit Claim** - Claim today's credits per account or for all enabled accounts from the Accounts page
- **Token Auto-Refresh** - Automatically refreshes tokens before expiry
- **API Key Management** - Create separate keys for OpenCode, Cherry Studio, etc.
- **Recoverable Key Management** - Uses SHA-256 hashes for authentication and stores an encrypted recoverable copy for the protected admin UI
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
3. Python 3.10 or newer installed, unless you use Docker. Miniconda with a dedicated environment is recommended for beginners.
4. Best to run this project and calling clients on the same machine.

Default Windows scan path:

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

Use `CB_AUTH_DIR` if your auth files are in a different directory.

## Installation and Startup

The following steps start from a machine that does not yet have Git, Conda, or the project source code installed.

### Step 1: Install the required tools

For a Python installation, install:

1. [Git](https://git-scm.com/downloads) to download and update the source code. The default installer options are suitable on Windows.
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) to install Python and create an isolated project environment. Python 3.10, 3.11, and 3.12 are supported; 3.12 is recommended.
3. Tencent Work Buddy / CodeBuddy. Sign in at least once and confirm that the account has available credits.

After installation, reopen PowerShell, Windows Terminal, or Anaconda Prompt and verify the commands:

```powershell
git --version
conda --version
```

If PowerShell cannot find `conda`, use **Anaconda Prompt / Miniconda Prompt** from the Start menu. You can also run `conda init powershell` there, then close and reopen PowerShell.

### Step 2: Clone the source code

Open a terminal in the directory where you want to store the project, then run:

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
```

Run the remaining commands from the `Buddy2api` directory. On PowerShell, verify the location with:

```powershell
Get-ChildItem README.md, requirements.txt, server.py
```

### Option A: Conda (recommended for beginners)

Create an isolated Python 3.12 environment named `buddy2api`:

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

When the server starts listening, open:

```text
http://127.0.0.1:8787
```

Press `Ctrl+C` in the terminal to stop it. On later starts, do not recreate the environment or reinstall everything:

```powershell
cd <your-project-path>\Buddy2api
conda activate buddy2api
python server.py
```

Use `conda env list` to see existing environments. Only run `conda create` again when you intentionally want a clean reinstall.

### Option B: Bundled startup scripts

If you do not want Conda, install Python 3.10 or newer from [python.org](https://www.python.org/downloads/). On Windows, select **Add Python to PATH** in the installer.

After cloning the repository and entering its directory, double-click `start.bat` or run:

```powershell
.\start.bat
```

The script first looks for Conda and uses or creates a Python 3.12 environment named `buddy2api`. It also checks common Miniconda and Anaconda install locations when PowerShell has not been initialized for Conda. Only when Conda is unavailable does it create a project-local `.venv`. It then installs missing dependencies and starts the server.

On Linux or macOS:

```bash
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
chmod +x start.sh
./start.sh
```

`start.sh` also prefers the `buddy2api` Conda environment and falls back to `.venv` only when Conda is unavailable.

### Option C: Docker

Docker users should also install Git and clone the repository first:

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
```

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

To start without mounting the Windows auth directory, provide a random admin token first:

```bash
export CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value"
docker compose up -d
```

Open browser at:

```text
http://127.0.0.1:8787
```

Note: Docker containers cannot magically scan the Windows C drive. The auth directory must be mounted as a volume. The scripts automate that read-only mount.

### After the first startup

For local browser access, the Web UI uses a same-origin HttpOnly admin cookie automatically. You usually do not need to paste the Admin Token manually. Then:

1. Open Accounts, rescan, and confirm that the Work Buddy / CodeBuddy account was imported.
2. Run the single-account test to confirm that model requests work.
3. Create a client key on API Keys. You can view and copy it again later from the Admin-protected list.
4. Configure the client with `http://127.0.0.1:8787/v1`, the new API key, and model `auto`.

For remote access or cookie fallback cases, set a fixed admin token before startup:

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value"
python server.py
```

### Updating to the latest version

Press `Ctrl+C` to stop the running server before updating.

Conda installation:

```powershell
cd <your-project-path>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python server.py
```

Windows `.venv` fallback installation (when Conda is unavailable):

```powershell
cd <your-project-path>\Buddy2api
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.bat
```

Docker installation:

```powershell
cd <your-project-path>\Buddy2api
git pull --ff-only
powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1
```

### Common installation problems

- `git` or `conda` is not recognized: close and reopen the terminal; Conda users can use Miniconda Prompt.
- Packages install into the wrong Python: always use `python -m pip` and confirm that `(buddy2api)` appears in the prompt first.
- `No module named ...`: reactivate the environment and run `python -m pip install -r requirements.txt`.
- Port `8787` is already in use: stop the older Buddy2api process or run `python server.py --port 8788`.
- No account appears in the Web UI: confirm that Work Buddy / CodeBuddy is signed in, check the default auth path, and set `CB_AUTH_DIR` when needed.
- Dependency downloads are slow or fail: confirm that PyPI is reachable and retry the install command; avoid mixing multiple Python or Conda environments.

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
/v1/responses
/v1/models
```

Regular clients can use **OpenAI Compatible / Chat Completions**. Codex and other Responses API clients can call `/v1/responses` directly.

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
| `CB_GATEWAY_MASTER_KEY` | Cross-platform credential master key; set a stable value for Docker or migrations |
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` | Local encryption key file used when no master key is configured |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | Request-log retention in days, default `90` |
| `CB_GATEWAY_MAX_BODY_BYTES` | Maximum JSON request size, default `10485760` |
| `CB_GATEWAY_CORS_ORIGINS` | Comma-separated browser origin allowlist; local origins by default |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | Set to `1` to allow API calls before creating a key; disabled by default |
| `CB_GATEWAY_SECURE_COOKIE` | Set to `1` to force the Secure flag on the admin cookie |
| `CB_GATEWAY_USER_AGENT` | Full outgoing User-Agent; defaults to the official CLI fingerprint `CLI/2.109.2 CodeBuddy/2.109.2`; set `codebuddy2openai/2.0` to revert to the historical UA |
| `CB_GATEWAY_IDE_VERSION` | CLI version used in the official fingerprint, default `2.109.2` |
| `CB_GATEWAY_STAINLESS_OS` | OS reported by the official fingerprint; inferred from the current platform by default |
| `CB_GATEWAY_STAINLESS_PACKAGE_VERSION` | Official SDK fingerprint package version, default `5.10.1` |
| `CB_GATEWAY_NODE_VERSION` | Official SDK fingerprint Node runtime version, default `v22.13.1` |
| `CB_AUTH_DIR` | Work Buddy / CodeBuddy auth file directory |
| `CB_HOST_AUTH_DIR` | Host auth directory used by Docker helper scripts |
| `CB_CONTAINER_AUTH_DIR` | Auth mount directory inside Docker, default `/auth` |

## Official request fingerprint

Upstream requests carry the full request-header fingerprint of the official Work Buddy / CodeBuddy CLI client (see `fingerprint.py`), matching the official client's outgoing requests:

- **Common**: `X-Requested-With: XMLHttpRequest`, region-aware `Origin` / `Referer` (`www.codebuddy.cn` or `www.workbuddy.ai`), `X-Product: SaaS`, `X-Domain`, plus `X-Request-ID` / `X-B3-*` / `b3` trace headers.
- **Chat**: `Authorization: Bearer`, `X-User-Id`, `X-Enterprise-Id`, `X-Tenant-Id`, `X-IDE-Type/Name/Version`, `x-codebuddy-request: 1`, `X-Agent-Intent: craft`, per-request `X-Conversation-*`, and the official SDK `x-stainless-*` headers.
- **Billing**: balance / check-in endpoints use a lean fingerprint (`Authorization`, `X-User-Id`, `X-Enterprise-Id`, `X-Tenant-Id`, `X-Domain`).
- **Refresh**: `X-Refresh-Token` only ever appears on the token-refresh endpoint, never on chat requests.

Missing fields follow the official CLI convention of `X-No-*` markers (e.g. `X-No-User-Id: 1`) instead of empty values. Everything can be overridden with `CB_GATEWAY_USER_AGENT` and the other variables listed above. The fingerprint covers request headers only; if the upstream also validates TLS fingerprints (JA3), an additional TLS-impersonation forwarding layer would be required.

## Data and Security

- Account tokens are encrypted before SQLite writes. Windows uses DPAPI; other platforms use the configured master key or a local `0600` key file.
- Plaintext credentials in older databases are encrypted automatically on first startup. Set a stable `CB_GATEWAY_MASTER_KEY` before moving databases or containers.
- Request logs are retained for 90 days by default; adjust `CB_GATEWAY_LOG_RETENTION_DAYS` when needed.
- API keys use SHA-256 hashes for authentication. A separate encrypted copy is available only through the Admin-protected management API.
- Keys created before this upgrade only have hashes and cannot be reconstructed. They continue to authenticate normally, but create a replacement if you need to copy one from the management UI.
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
