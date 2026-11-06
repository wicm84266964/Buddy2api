# Buddy2api 2.0

[English](README_EN.md) | [中文](README.md)

> Local consumer AI clients → one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, Qoder, QwenWork, TraeWork, iFlytek Loomy, and SenseTime Xiaohuanxiong are on by default; pick one in the UI dropdown. Each request stays on one channel. QClaw is retired; its credits moved to WorkBuddy.

Release **2.2.18**. Default channels include WorkBuddy domestic and international, Qoder CN and international, QwenWork, TraeWork, Loomy, and Xiaohuanxiong. Channels without a stored login are omitted from the model list. Local use only, and only with accounts you personally own. Do not expose this on the public internet, and do not share credentials, API keys, or the database.

**Your own accounts only. Do not use SMS-code platforms, bulk registration, or any batch method to farm credits.** Do not expose the gateway to other people, claim rewards for them, or resell quota.

Default loopback startup opens the management page without an Admin Token. Restarting does not invalidate local access. Repeated startup opens the existing instance; a database can only be used by one process. Use `--no-browser` for background services. Setting `--admin-token` or `CB_GATEWAY_ADMIN_TOKEN` enables explicit token authentication and is required for non-loopback listeners. Enter that token in the management settings. Client API keys and upstream account credentials are unchanged.

## What is this?

Buddy2api listens on `http://127.0.0.1:8787/v1`. You stay signed into the official apps; this gateway imports those sessions and forwards chat. Typical clients use Chat Completions. Codex uses `/v1/responses`. A key can stay on one channel, or use automatic channel and pick the vendor with a `channel/model` id.

Five channels are on by default. A channel with no local login shows empty on Accounts; nothing is imported until you click Import. `qclaw` in `CB_GATEWAY_PROVIDERS` is ignored.

Qoder CN reads `%APPDATA%\com.qodercn.app.stable\auth.v1.dat`. Qoder international reads `%APPDATA%\com.qoder.app.stable\auth.v1.dat`. Their login files, model catalogs, and tokens are not interchangeable. Quota is shown from claim results and the official balance. Tool calls are returned to the local client.

Loomy reads a local `auth-session.json`. Its daily balance is granted upstream; one-click claim only reads that balance. Long Loomy chats are trimmed to a 160k-token budget before send. Xiaohuanxiong reads a local `auth.json`, and one-click claim triggers the daily grant. The current client stores it at `%USERPROFILE%\.office-raccoon\agent\config\auth.json`; the old `%USERPROFILE%\.box-agent\config\auth.json` is still scanned. When the file has no user id, accounts are separated by the subject in the login token, so importing a second login does not replace the first. If the official client revokes the previous login on sign-out, that saved account still dies and must be imported while it is signed in. Neither channel uses the WorkBuddy CLI header set.

```powershell
python server.py
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy domestic | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| WorkBuddy international | on | Same folder; `*.workbuddy.ai` accounts move to `workbuddy-i` |
| Qoder CN | on | `%APPDATA%\com.qodercn.app.stable\auth.v1.dat` |
| Qoder international | on | `%APPDATA%\com.qoder.app.stable\auth.v1.dat` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | `%APPDATA%\TRAE SOLO CN\User\globalStorage`; newer Trae CN also uses `%APPDATA%\Trae CN\User\globalStorage` |
| iFlytek Loomy | on | `%PUBLIC%\Loomy\*\userData\auth-session.json` |
| SenseTime Xiaohuanxiong | on | `%USERPROFILE%\.office-raccoon\agent\config\auth.json`; old clients still use `%USERPROFILE%\.box-agent\config\auth.json` |

Narrow with `CB_GATEWAY_PROVIDERS=workbuddy` if you only want one.

## Before you start

1. **An empty Accounts page after startup is expected.** 2.0 does not import on boot. For WorkBuddy, use **Browser login** and sign in on the official page. You can also pick a channel → Detect → Import. **Renew and claim** refreshes saved logins and claims today's points. A single account can use **Renew** in its row menu. Scheduled renewal and check-in live in Settings and stay off until you turn them on and save.
2. **One API key is one channel.** Create the key with a channel selected. A WorkBuddy key uses `auto` / `glm-5.2`; a Qoder key uses `auto` or `Qwen3.8-Flash`; a QwenWork key uses `auto` or `qwork-advanced`; a TraeWork key uses `auto` or `qwen-3.7-plus`. Mismatched model/key returns 400 or 403 — there is no cross-vendor failover. Automatic channel uses a prefixed id such as `qoder/DeepSeek-Flash` for that hop.
3. **HTTP 503 `channel_unavailable`** means that channel has no imported account.
4. **Run QwenWork with `python server.py` on Windows.** A Linux Docker container cannot decrypt its DPAPI files; the UI says so. WorkBuddy can stay on Docker.
5. If the chat client is itself in Docker, Base URL is `http://host.docker.internal:8787/v1`.

## Install

1. [Git](https://git-scm.com/downloads), [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) (Python 3.12), and sign into Work Buddy / CodeBuddy at least once.
2. Reopen the terminal, then:

```powershell
git --version
conda --version
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

3. Open http://127.0.0.1:8787 → Accounts → Detect → Import → Test → API Keys (automatic channel by default; pick one channel to pin the key) → point your client at `http://127.0.0.1:8787/v1`.

Windows script: `.\start.bat`. Docker helper: `.\start-docker-win.ps1` (WorkBuddy mount; use native Python for QwenWork). That helper builds from the local checkout. The prebuilt image is `ghcr.io/wicm84266964/buddy2api:v2.2.18` (`linux/amd64` only).

Later starts: `conda activate buddy2api` then `python server.py` in the project directory.

## Docker

The official image is published only to GHCR, not Docker Hub: `ghcr.io/wicm84266964/buddy2api:v2.2.18`. A stable release also moves `latest`; prereleases do not. Pin the version tag. The image is `linux/amd64` only.

The first push can create a private GitHub Packages container. The repository owner must change that package to public once before anonymous `docker pull` works.

Do not publish port 8787 on the public internet. Compose keeps the bind at `127.0.0.1:8787`. A Linux image cannot decrypt Windows DPAPI files. Import QwenWork, Loomy, and Xiaohuanxiong with local `python server.py`, then mount the data directory into the container.

From a checkout:

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value"
docker compose pull
docker compose up -d
```

`docker-compose.yml` pins the current tag and still has `build: .`. Use `docker compose up -d --build` or `.\start-docker-win.ps1` to build from source.

Without cloning:

```powershell
docker pull ghcr.io/wicm84266964/buddy2api:v2.2.18
docker run -d --name buddy2api --restart unless-stopped `
  -p 127.0.0.1:8787:8787 `
  -v buddy2api-data:/app/data `
  -e CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value" `
  -e CB_DOCKER=1 `
  ghcr.io/wicm84266964/buddy2api:v2.2.18
```

## FAQ

- `conda` not found: use Miniconda Prompt, or `conda init powershell` and reopen the terminal.
- `No module named ...`: activate `buddy2api`, then `python -m pip install -r requirements.txt`.
- Port 8787 in use: stop the old process or `python server.py --port 8788`. After a one-click update, use the top-bar Close button if there is no console window.
- No accounts in the UI: import has not been run yet.
- Key create fails: the channel dropdown is required.
- 403 `key_channel_mismatch`: the model prefix does not match the key’s channel.
- 400 `unknown_model`: that model does not belong to this key’s channel.

## Update

The management page checks for a newer `main` in the background (jsDelivr first). A top-bar **Update to vx.y.z** button runs `git pull --ff-only`, reinstalls requirements, and restarts. That pull and install reuse `HTTPS_PROXY`, `ALL_PROXY`, or an explicit OS proxy address. The button does not hardcode a port or change Git config. A PAC script with no explicit address still leaves Git connecting directly. A failed check stays silent. Docker images, zip copies, and non-`main` checkouts keep using a manual update. For Docker, pull the tag pinned in compose and restart: `docker compose pull` then `docker compose up -d`. To preview the button on an already-current install, start with `CB_GATEWAY_FAKE_LATEST=9.9.9`; the click does not pull or restart.

## Upgrade from 1.4.x

The database migrates on startup. Existing keys stay on `workbuddy`. Startup no longer auto-imports; empty channel is 503; new keys must pick a channel; the official-balance column shows credits only.

## Client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the UI, bound to one channel |
| Model | WorkBuddy, Loomy, and Xiaohuanxiong use `auto`; Qoder uses `auto` or `Qwen3.8-Flash`; QwenWork uses `auto` or `qwork-advanced`; TraeWork uses `auto` or `qwen-3.7-plus`. Move old QClaw keys to WorkBuddy |

Unprefixed `auto` follows the key’s channel. Use a separate key per channel. On the Models page, “一键读取供应模型” refreshes each channel’s supplier list separately; a TraeWork-only id such as Doubao is never merged into WorkBuddy.

### Account priority, saving, and request logs

For automatic-channel keys, a bare alias or `auto` chooses the matching channel whose active account has the highest priority. Larger numbers win; ties keep the default channel order. Channel-bound keys and `channel/model` requests keep their explicit channel. Retry budgets cover the channel's whole account pool instead of stopping after three accounts. Each channel retains its existing rules for non-retryable errors and streams that have already started output.

Accounts → **保存全部修改** saves every edited row, including rows hidden by filters. Failed rows keep their edits for retry, and saving one row preserves other drafts. Request logs show the requested alias, selected channel, and resolved model, and support searching by resolved model. Historical logs keep their original fields without backfilling mappings.

### Model capacity discovery

Refreshing supplier models now retains upstream input/output capacities. `/v1/models` exposes `context_window` and `max_output_tokens`; WorkBuddy's `maxInputTokens` and `maxOutputTokens` map to these fields, rather than treating its desktop `contextWindow.defaultLength` as the maximum. DSH custom providers can import these capacities through **Fetch available models**, then add and save the selected models. Existing configured models are not overwritten automatically.

Missing fields independently fall back to 262,144 context tokens and 32,768 output tokens on every channel. The per-field `capacity_source` is `catalog` or `fallback`. Fallbacks are configuration defaults, not verified upstream limits. Explicit `max_tokens` and `max_completion_tokens` requests are clamped only when the catalog contains a known output limit; converted Responses `max_output_tokens` requests use the same path. Omitted budgets stay omitted, and unknown capacities do not impose a hard cap. Reaching a configured output budget can still produce `length`; discovery does not prevent all long-task truncation.

### Reasoning effort

WorkBuddy's collected responses, including the default tool-stall retry path, reject partial text without completion metadata instead of synthesizing a successful `stop`. An explicit `finish_reason` followed by EOF remains valid without `[DONE]`. A `[DONE]` event alone does not make text without a finish reason complete. This validation does not determine whether a model's explicit `stop` is premature or resolve every long-session stall.

Agent clients can send top-level `reasoning_effort` to Chat Completions and the standard `reasoning: {"effort": "high"}` object to Responses. Compatibility forms used by OpenCode, DSH, Cherry, and Claude-style clients are also accepted: `reasoning.effort`, `reasoningEffort`, `thinking.type`, `thinking.effort`, `output_config.effort`, and `enable_thinking`. Accepted levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; `off` is an alias for `none`.

| Channel | Effective capability |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash supports `low` / `high` / `max`; standard levels are projected onto those tiers. The default is `high` and can be disabled with `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` |
| QwenWork | The protocol exposes only an `is_reasoning` switch. `none` disables it and any other explicit tier enables it; distinct effort levels are unavailable |
| TraeWork | The current session protocol has no verified reasoning control field, so effort selection is not supported |

Chat streams preserve `reasoning_content`. Responses streams expose standard `response.reasoning_summary_*` events and accept valid reasoning-only completions.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-your-key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Environment

`CB_GATEWAY_PROVIDERS` (default `workbuddy,workbuddy-i,qwenwork,qoder,qoder-i,traework,loomy,xiaohuanxiong`; `qclaw` is ignored), `CB_GATEWAY_AUTO_IMPORT` (default `0`), `CB_AUTH_DIR` / `CB_QODER_USER_DATA_DIR` / `CB_QODER_I_USER_DATA_DIR` / `CB_QWENWORK_AUTH_DIR` / `CB_TRAEWORK_AUTH_DIR` / `CB_LOOMY_AUTH_DIR` / `CB_XHX_AUTH_DIR`, `CB_GATEWAY_ADMIN_TOKEN`, `CB_GATEWAY_MASTER_KEY`.

`CB_GATEWAY_DEFAULT_REASONING_EFFORT` controls the default reasoning effort for WorkBuddy DeepSeek V4 Pro/Flash. It accepts `low`, `high`, or `max`, defaults to `high`, and can be disabled with `off`. A Responses `reasoning.effort` or Chat Completions `reasoning_effort` value overrides the default.

Keep `--host 127.0.0.1`. Do not share the database, auth folders, or key screenshots.

## License

MIT
