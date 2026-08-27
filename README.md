# Buddy2api 2.0

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、QClaw、千问办公（QwenWork）、TraeWork 四个通道；管理页下拉选其中一个。一次请求只走一个通道。

当前版本 **2.1.1**。这个项目只适合本机自用，不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`，管理页把 Key 类型选成 Codex 时会做一轮内容清洗。

四个通道默认都开。没装、没登录的通道，账号页检测为空，不会自动入库。

```powershell
python server.py
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | 开 | `%APPDATA%\QClaw` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |

路径不对时可用 `CB_AUTH_DIR`、`CB_QCLAW_AUTH_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR` 指定。四个通道的登录文件不要混在同一个目录。只要其中一家时，可设 `CB_GATEWAY_PROVIDERS=workbuddy` 收窄。

## 注意事项

按下面「安装与启动」即可。这几条是 2.0 里最容易踩空的：

1. **启动后账号页是空的，这是正常的。** 默认不再自动入库。到「账号」页：选通道 → 重新检测 → 一键导入。四个通道都能选。
2. **一把 API Key 只打一个通道。** 创建时必须选通道。WorkBuddy 的 Key 发 `auto` / `glm-5.2`；QwenWork 的 Key 发 `auto` 或 `qwork-advanced`；TraeWork 的 Key 发 `auto` 或 `qwen-3.7-plus`。通道和模型对不上会 400 或 403，不会帮你转到另一家。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QClaw / QwenWork 请在 Windows 上直接跑 `python server.py`。** Linux Docker 读不了这两家用 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
5. 本项目和聊天客户端最好在同一台电脑。客户端如果跑在 Docker 里，Base URL 填 `http://host.docker.internal:8787/v1`，不要填容器自己的 `127.0.0.1`。

## 安装与启动

还没装环境时按这几步走。已经有虚拟环境的，装完 `requirements.txt` 后执行 `python server.py` 即可。

### 1. 安装工具

1. [Git](https://git-scm.com/downloads)，Windows 保持默认选项
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)，推荐 Python 3.12
3. 先打开并登录你要用的官方客户端（至少 Work Buddy / CodeBuddy）

装完后**重新打开** PowerShell、Windows Terminal 或 Anaconda Prompt：

```powershell
git --version
conda --version
```

找不到 `conda` 时，用开始菜单里的 **Anaconda Prompt / Miniconda Prompt**。也可以在那里执行 `conda init powershell`，关掉窗口再开。

### 2. 克隆项目

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, requirements.txt, server.py
```

后面的命令都要在这个目录里执行。

### 3. 用 Conda 启动（推荐）

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

看到监听信息后，浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务：回到终端按 `Ctrl+C`。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python server.py
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\start.bat`。Linux / macOS：`chmod +x start.sh && ./start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`。
- **Docker：** `powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1`。本机没有 WorkBuddy 登录目录时脚本仍会启动。容器下拉里仍有四个通道，但 QClaw / QwenWork 请用上面的 `python server.py`。TraeWork 登录文件不是 DPAPI，本机 `python server.py` 导入后 Docker 也能用库里的 token。

### 第一次打开网页之后

本机浏览器一般会自动带上管理 Cookie，不用粘贴 Token。

1. 打开「账号」。下拉里选 WorkBuddy / QClaw / 千问办公 / TraeWork，点「重新检测」，再点「一键导入本机登录」。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，**先选同一个通道**再创建。给 Codex 用时 Key 类型选 Codex，接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy 用 `auto` 即可；QClaw 用 `auto`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python server.py
```

### 更新

先 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python server.py
```

## 常见问题

- `git` 或 `conda` 不是内部命令：关掉终端重开；Conda 用户改用 Miniconda Prompt。
- `No module named ...`：先 `conda activate buddy2api`，再 `python -m pip install -r requirements.txt`。
- 下载依赖很慢：确认能访问 PyPI，不要混用好几个 Python。
- 端口 8787 被占用：关掉旧的 Buddy2api，或 `python server.py --port 8788`。
- 网页里一个账号都没有：还没导入。选对通道再检测；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`。
- 创建 Key 失败：没选通道。
- 客户端 503 `channel_unavailable`：这个 Key 绑定的通道还没有可用账号。
- 客户端 403 `key_channel_mismatch`：模型带了别的通道前缀，和当前 Key 不一致。
- 客户端 400 `unknown_model`：模型不属于这把 Key 的通道。换 Key，或改成该通道认识的 id。

## 从 1.4.x 升级

启动时会自动改数据库。旧 Key 视为绑在 `workbuddy` 上，原来的 `auto` / `glm-5.2` 还能用。

和 1.4 不同的地方：启动不再自动导入账号；空仓是 503 而不是普通 `server_error`；新建 Key 必须选通道；官方余额只显示积分，不把各厂数字加在一起。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 管理页创建，已绑定通道 |
| 模型 | WorkBuddy：`auto` / `glm-5.2`。QClaw：`auto` 或 `qclaw/default`。QwenWork：`auto` 或 `qwork-advanced`。TraeWork：`auto` 或 `qwen-3.7-plus` |
| Stream | 建议开 |

接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`。没加前缀的 `auto` 走这把 Key 绑定的通道。Codex 用 Responses 接口；管理页选 Codex 类型的 Key 会按 Codex 特征 prompt 做清洗（其它客户端借用这把 Key、但没有 Codex 特征时不改写）。

### 思考强度

智能体可以在 Chat Completions 中发送顶层 `reasoning_effort`，在 Responses 中发送标准的 `reasoning: {"effort": "high"}`。网关也兼容 OpenCode、DSH、Cherry 和 Claude 风格的 `reasoning.effort`、`reasoningEffort`、`thinking.type`、`thinking.effort`、`output_config.effort`、`enable_thinking` 等写法。可用档位为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`、`ultra`；`off` 等同于 `none`。

```json
{
  "model": "deepseek-v4-pro",
  "messages": [{"role": "user", "content": "分析这个问题"}],
  "reasoning_effort": "high"
}
```

| 通道 | 实际能力 |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash 支持 `low` / `high` / `max`，标准档位会投影到这三档；未指定时默认 `high`，可用 `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` 关闭默认 |
| QClaw | 统一转换成 `reasoning_effort` 后透传；具体档位是否生效由所选上游模型决定，不额外注入默认值 |
| QwenWork | 协议只有 `is_reasoning` 开关；`none` 关闭，其它显式档位开启，无法区分多档强度 |
| TraeWork | 当前会话协议没有可验证的思考控制字段，因此暂不支持调档 |

Chat 流会保留 `reasoning_content`。Responses 流会转换成标准的 `response.reasoning_summary_*` 事件，仅有推理、没有最终正文的有效响应也会正常完成。

OpenCode 示例（WorkBuddy Key）：

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-你的key"
      },
      "models": {
        "auto": { "name": "WorkBuddy Auto" },
        "glm-5.2": { "name": "GLM-5.2" }
      }
    }
  }
}
```

```powershell
opencode run -m workbuddy/auto "你好"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-你的key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

QwenWork、QClaw、TraeWork 各用自己那把 Key，不要混用。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 自动生成 | 管理 Token；本机网页通常用 Cookie |
| `--no-admin-auth` | 关 | 关掉管理鉴权，只适合本机临时试 |

## 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,qclaw,qwenwork,traework`。只想留一家时再改 |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动导入。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取间隔，默认 `800` |
| `CB_GATEWAY_DEFAULT_REASONING_EFFORT` | WorkBuddy DeepSeek V4 Pro/Flash 的默认思考强度，支持 `low` / `high` / `max`，默认 `high`；设为 `off` 可关闭默认值。Responses 的 `reasoning.effort` 或 Chat Completions 的 `reasoning_effort` 会覆盖它 |
| `CB_AUTH_DIR` | WorkBuddy 登录目录 |
| `CB_QCLAW_AUTH_DIR` | QClaw 登录目录 |
| `CB_QWENWORK_AUTH_DIR` | QwenWork 登录目录 |
| `CB_TRAEWORK_AUTH_DIR` | TraeWork `storage.json` 所在目录 |
| `CB_HOST_AUTH_DIR` | Docker 脚本用的本机 WorkBuddy 目录 |
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理 Token |
| `CB_GATEWAY_DB_PATH` | 数据库路径 |
| `CB_GATEWAY_MASTER_KEY` | 跨系统搬数据库时的加密主密钥 |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 日志保留天数，默认 `90` |
| `CB_GATEWAY_USER_AGENT` | 只影响 WorkBuddy 出站头，默认 `CLI/2.109.2 CodeBuddy/2.109.2` |

## 数据和安全

- 账号 Token 写入前会加密。Windows 用系统 DPAPI。
- 不要把 `*.db`、登录目录、日志、带 Key 的截图发出去。
- 不要把服务绑到公网。保持 `127.0.0.1`。

## License

MIT
