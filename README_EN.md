# Buddy2api 2.0

[English](README_EN.md) | [中文](README.md)

> Local consumer AI clients → one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, QClaw, QwenWork, and TraeWork are on by default; pick one in the UI dropdown. Each request stays on one channel.

Release **2.1.3**. Local use only. Do not expose this on the public internet, and do not share credentials, API keys, or the database.

## What is this?

Buddy2api listens on `http://127.0.0.1:8787/v1`. You stay signed into the official apps; this gateway imports those sessions and forwards chat. Typical clients use Chat Completions. Codex uses `/v1/responses`; create the key as type Codex in the UI to enable Codex prompt sanitization.

All four channels are on by default. A channel with no local login shows empty on Accounts; nothing is imported until you click Import.

```powershell
python server.py
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | on | `%APPDATA%\QClaw` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |

Narrow with `CB_GATEWAY_PROVIDERS=workbuddy` if you only want one.

## Before you start

1. **An empty Accounts page after startup is expected.** 2.0 does not import on boot. Pick a channel → Detect → Import. All four channels are in the dropdown.
2. **One API key is one channel.** Create the key with a channel selected. A WorkBuddy key uses `auto` / `glm-5.2`; a QwenWork key uses `auto` or `qwork-advanced`; a TraeWork key uses `auto` or `qwen-3.7-plus`. Mismatched model/key returns 400 or 403 — there is no cross-vendor failover.
3. **HTTP 503 `channel_unavailable`** means that channel has no imported account.
4. **Run QClaw / QwenWork with `python server.py` on Windows.** A Linux Docker container cannot decrypt those DPAPI files; the UI says so. WorkBuddy can stay on Docker.
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

3. Open http://127.0.0.1:8787 → Accounts → Detect → Import → Test → API Keys (select a channel; pick Codex if the client is Codex) → point your client at `http://127.0.0.1:8787/v1`.

Windows script: `.\start.bat`. Docker helper: `.\start-docker-win.ps1` (WorkBuddy mount; use native Python for QClaw/QwenWork).

Later starts: `conda activate buddy2api` then `python server.py` in the project directory.

## FAQ

- `conda` not found: use Miniconda Prompt, or `conda init powershell` and reopen the terminal.
- `No module named ...`: activate `buddy2api`, then `python -m pip install -r requirements.txt`.
- Port 8787 in use: stop the old process or `python server.py --port 8788`.
- No accounts in the UI: import has not been run yet.
- Key create fails: the channel dropdown is required.
- 403 `key_channel_mismatch`: the model prefix does not match the key’s channel.
- 400 `unknown_model`: that model does not belong to this key’s channel.

## Upgrade from 1.4.x

The database migrates on startup. Existing keys stay on `workbuddy`. Startup no longer auto-imports; empty channel is 503; new keys must pick a channel; the official-balance column shows credits only.

## Client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the UI, bound to one channel |
| Model | WorkBuddy `auto`; QClaw `auto`; QwenWork `qwork-advanced` |

Unprefixed `auto` follows the key’s channel. Use a separate key per channel. On the Models page, “一键读取供应模型” refreshes each channel’s supplier list separately; a TraeWork-only id such as Doubao is never merged into WorkBuddy.

### Reasoning effort

Agent clients can send top-level `reasoning_effort` to Chat Completions and the standard `reasoning: {"effort": "high"}` object to Responses. Compatibility forms used by OpenCode, DSH, Cherry, and Claude-style clients are also accepted: `reasoning.effort`, `reasoningEffort`, `thinking.type`, `thinking.effort`, `output_config.effort`, and `enable_thinking`. Accepted levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; `off` is an alias for `none`.

| Channel | Effective capability |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash supports `low` / `high` / `max`; standard levels are projected onto those tiers. The default is `high` and can be disabled with `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` |
| QClaw | The control is normalized to `reasoning_effort` and forwarded. Whether a tier takes effect depends on the selected upstream model; no gateway default is injected |
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

`CB_GATEWAY_PROVIDERS` (default `workbuddy,qclaw,qwenwork,traework`), `CB_GATEWAY_AUTO_IMPORT` (default `0`), `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR` / `CB_TRAEWORK_AUTH_DIR`, `CB_GATEWAY_ADMIN_TOKEN`, `CB_GATEWAY_MASTER_KEY`.

`CB_GATEWAY_DEFAULT_REASONING_EFFORT` controls the default reasoning effort for WorkBuddy DeepSeek V4 Pro/Flash. It accepts `low`, `high`, or `max`, defaults to `high`, and can be disabled with `off`. A Responses `reasoning.effort` or Chat Completions `reasoning_effort` value overrides the default.

Keep `--host 127.0.0.1`. Do not share the database, auth folders, or key screenshots.

## License

MIT
