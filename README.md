# Buddy2api

[English](README_EN.md) | 中文

> 把本机已经登录的腾讯 Work Buddy / CodeBuddy 账号接成 OpenAI 兼容 API，方便在 OpenCode、OpenClaw、Cherry Studio、NextChat 等工具里使用。

## 这是什么？

Work Buddy 2 API 是一个本地网关。它会扫描本机 Work Buddy / CodeBuddy 桌面端或插件保存的登录凭据，把请求转发到腾讯的模型接口，并在本机提供标准 OpenAI 兼容接口。

简单说：你已经在 Work Buddy 里登录并且有可用额度，这个项目把这份额度通过 `http://127.0.0.1:8787/v1` 暴露出来，让其他支持 OpenAI API 的客户端也能调用。

这个项目主要用于本地自用和测试。不要公开部署，不要共享给别人用，也不要把自己的登录凭据、API Key 或数据库文件发出去。

## 功能

- **OpenAI 兼容接口**：支持 `/v1/chat/completions`、`/v1/responses` 和 `/v1/models`
- **流式输出**：支持 SSE 流式响应，也支持非流式聚合响应
- **自动导入账号**：启动时扫描本机 Work Buddy / CodeBuddy 的 auth 文件
- **多账号路由**：支持多个账号，优先级高的账号先用；同优先级会尽量固定当前账号，失败后自动切换
- **账号状态诊断**：支持启用/禁用、权重、优先级、单账号测试和 token 刷新
- **官方真实余额**：账号页可读取 Work Buddy 官方资源余额、30 天内即将到期额度和额度包明细
- **余额快照估算**：官方余额读取失败时，可填写账号当前剩余额度作为本地备用估算
- **手动领取每日积分**：账号页支持单账号领取，也支持一键领取所有已启用账号的今日积分
- **Token 自动刷新**：登录 token 快过期时自动刷新并写回数据库
- **API Key 管理**：给 OpenCode、Cherry Studio 等客户端单独创建 key
- **Key 可恢复管理**：使用 SHA-256 哈希鉴权，并加密保存完整 Key，可在受保护的管理页再次查看和复制
- **模型权限控制**：可以限制某个 key 只能使用指定模型
- **每日请求限额**：可以给 key 设置每日请求次数上限
- **Dashboard**：查看健康状态、官方额度汇总、到期提醒、请求趋势、模型排行、账号状态、Key 使用和最近日志
- **Web 管理界面**：账号、API Keys、模型、日志、设置都可以在网页里管理
- **请求日志**：记录模型、token、credit、耗时、状态码和错误信息，支持筛选、搜索、分页和详情展开
- **接入向导**：设置页提供 OpenCode / OpenClaw、sub2api Docker、Cherry Studio、NextChat 和 curl 的接入参数
- **Function Calling 透传**：支持 `tools` / `tool_calls`
- **模型别名**：内置常见别名，也支持自己扩展

## 前提条件

1. 本机已经安装并登录腾讯 Work Buddy / CodeBuddy。
2. 登录账号还有可用模型额度。
3. 本机安装 Python 3.10 或更高版本，或使用 Docker。新手推荐安装 Miniconda，并创建独立环境。
4. 本项目和调用客户端最好都运行在同一台机器上。

Windows 默认扫描路径类似：

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

如果你的 auth 文件在别的目录，可以用 `CB_AUTH_DIR` 指定。

## 安装与启动

下面的命令适合第一次接触 Git、Conda 和 Python 项目的用户。已经熟悉这些工具的用户可以直接选择自己常用的虚拟环境。

### 第一步：安装基础工具

使用 Python 方式运行需要安装：

1. [Git](https://git-scm.com/downloads)：用于下载和更新源码。Windows 安装时保持默认选项即可。
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)：用于安装 Python 并为本项目创建独立环境。安装 Python 3.10、3.11 或 3.12 均可，推荐 3.12。
3. 腾讯 Work Buddy / CodeBuddy：安装后至少登录一次，并确认账号有可用额度。

安装完成后，重新打开 PowerShell、Windows Terminal 或 Anaconda Prompt，检查命令是否可用：

```powershell
git --version
conda --version
```

如果 PowerShell 提示找不到 `conda`，请先使用开始菜单里的 **Anaconda Prompt / Miniconda Prompt**。也可以在该终端执行 `conda init powershell`，关闭并重新打开 PowerShell。

### 第二步：克隆源码

打开终端，进入你准备存放项目的目录，然后执行：

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
```

后续命令都要在 `Buddy2api` 项目目录中执行。可以用下面的命令确认当前目录正确：

```powershell
Get-ChildItem README.md, requirements.txt, server.py
```

### 方式 A：使用 Conda（新手推荐）

创建一个名为 `buddy2api` 的独立 Python 3.12 环境：

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

看到 `[启动]` 或 Uvicorn 监听信息后，在浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务请回到终端按 `Ctrl+C`。以后再次启动时，不需要重新创建环境和安装依赖，只需执行：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python server.py
```

可以用 `conda env list` 查看已有环境。除非准备彻底重装，不要重复执行 `conda create`。

### 方式 B：使用自带启动脚本

不想安装 Conda 时，也可以先从 [Python 官网](https://www.python.org/downloads/) 安装 Python 3.10 或更高版本。Windows 安装界面需要勾选 **Add Python to PATH**。

克隆源码并进入项目目录后，Windows 可以双击 `start.bat`，也可以在 PowerShell 执行：

```powershell
.\start.bat
```

脚本会优先查找 Conda，并自动使用或创建名为 `buddy2api` 的 Python 3.12 环境；即使 PowerShell 尚未执行 `conda init`，脚本也会检查常见的 Miniconda/Anaconda 安装目录。只有找不到 Conda 时才会在项目目录创建 `.venv`。随后脚本会安装缺少的依赖并启动服务。

Linux / macOS 使用：

```bash
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
chmod +x start.sh
./start.sh
```

`start.sh` 同样优先使用或创建 `buddy2api` Conda 环境，找不到 Conda 时才回退到 `.venv`。

### 方式 C：Docker

Docker 用户也需要先安装 Git 并克隆项目：

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
```

Windows Docker Desktop 推荐用脚本启动，它会自动找到当前 Windows 用户的 Work Buddy 登录目录，并只读挂载到容器内的 `/auth`：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1
```

如果你是在 WSL 里操作 Docker：

```bash
chmod +x start-docker-wsl.sh
./start-docker-wsl.sh
```

这两个脚本默认寻找：

```text
C:\Users\<你的用户名>\AppData\Local\CodeBuddyExtension\Data\Public\auth
```

容器内会看到：

```text
/auth
```

所以 Web UI 的“重新检测 / 一键导入本机登录”可以直接发现账号，不需要手动粘贴 `.info`。

如果只是启动服务，不自动挂载 Windows 登录目录，需要先提供随机管理 Token：

```bash
export CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value"
docker compose up -d
```

启动后访问：

```text
http://127.0.0.1:8787
```

注意：Docker 容器不能凭空扫描 Windows 的 C 盘，必须通过 volume 挂载。脚本做的就是自动找路径并挂载，挂载方式是只读的。

### 首次启动后

打开 Web UI 后，本机浏览器会自动使用同源 HttpOnly Cookie 完成管理认证，通常不需要手动粘贴 Admin Token。然后按照以下顺序操作：

1. 在「账号」页面点击重新检测，确认已发现并导入 Work Buddy / CodeBuddy 账号。
2. 使用单账号测试确认账号可以正常请求模型。
3. 在「API Keys」页面创建客户端密钥；之后仍可在受 Admin 鉴权保护的列表中查看和复制完整密钥。
4. 在客户端中填写 `http://127.0.0.1:8787/v1`、刚创建的 API Key 和模型 `auto`。

如果你要远程访问或遇到 Cookie 异常，可以在启动前固定一个管理 token 作为备用：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请替换为足够长的随机值"
python server.py
```

### 更新到最新版

先按 `Ctrl+C` 停止正在运行的服务，再进入项目目录更新源码。

Conda 安装方式：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python server.py
```

Windows `.venv` 回退方式（设备未安装 Conda 时）：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.bat
```

Docker 安装方式：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1
```

### 常见安装问题

- `git` 或 `conda` 提示“不是内部或外部命令”：关闭当前终端并重新打开；Conda 用户可改用 Miniconda Prompt。
- `pip` 安装到了错误的 Python：始终使用 `python -m pip`，并先确认命令行前面显示 `(buddy2api)`。
- `No module named ...`：重新激活环境并执行 `python -m pip install -r requirements.txt`。
- 端口 `8787` 被占用：关闭旧的 Buddy2api 进程，或使用 `python server.py --port 8788` 启动到其他端口。
- Web UI 没有发现账号：先确认 Work Buddy / CodeBuddy 已登录；再检查默认 auth 路径，必要时通过 `CB_AUTH_DIR` 指定目录。
- 下载依赖很慢或失败：先确认网络可以访问 PyPI，再重新执行安装命令；不要混用多个 Python 或 Conda 环境。

## 使用流程

1. 先打开 Work Buddy / CodeBuddy，确认已经登录。
2. 启动本项目。
3. 在 Web UI 的「账号」页面确认账号已导入。
4. 在「API Keys」页面创建一个给客户端用的 key。
5. 在 OpenCode、OpenClaw、Cherry Studio、NextChat 等客户端里填入 Base URL 和 API Key。

「账号」页面会优先读取 Work Buddy 官方资源余额，并展示 30 天内即将到期额度和每个额度包的周期、剩余、已用、到期时间。每天领取的 150 积分会作为官方额度资源进入明细，实际到期时间以官方返回为准，通常约 1 个月。

Dashboard 会汇总所有已启用账号的官方余额、30 天内即将到期额度、低余额提醒和旧缓存状态。官方额度读取有本地短缓存，重复打开页面不会频繁请求官方接口；手动刷新和领取后会强制更新。

如果官方余额接口暂时失败，也可以在“本地快照”里填入 Work Buddy 当时显示的剩余额度并保存。它只作为备用估算，之后按保存以后新增的 `usage.credit` 扣减。

「账号」页面也提供手动领取今日积分按钮，可以单账号领取，也可以对所有已启用账号一键领取。这个动作不会定时执行；如果接口返回今日已领、活动不可用或账号失效，页面会直接显示对应结果。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Web UI「API Keys」页面创建 |
| Model | `auto` / `glm-5.2` / `glm-5.1` / `kimi-k2.7` / `deepseek-v4-pro` / `deepseek-v4-flash` |
| Stream | 建议开启 |

当前实现以下 OpenAI 兼容接口：

```text
/v1/chat/completions
/v1/responses
/v1/models
```

普通客户端可选择 **OpenAI Compatible / Chat Completions**；Codex 等固定使用 Responses API 的客户端可直接调用 `/v1/responses`。

如果调用方跑在 Docker 容器里，容器内的 `127.0.0.1` 指向容器自身，不是 Windows 主机。此时 Base URL 通常要填：

```text
http://host.docker.internal:8787/v1
```

这些常见客户端配置也可以直接在 Web UI「设置」页的接入向导里复制。

### OpenCode 示例

在 OpenCode 里添加一个 OpenAI-compatible provider：

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

调用：

```powershell
opencode run -m workbuddy/auto "你好"
```

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-xxxxx" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

## 启动参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--admin-token` | 自动生成 | 管理 API Token，本机 Web UI 通常自动使用 Cookie |
| `--no-admin-auth` | `false` | 关闭管理 API 鉴权，仅建议本机临时测试 |

## 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理后台 Token |
| `CB_GATEWAY_DB_PATH` | SQLite 数据库路径 |
| `CB_GATEWAY_MASTER_KEY` | 跨平台凭据加密主密钥；Docker/迁移场景建议固定配置 |
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` | 未配置主密钥时使用的本地加密密钥文件路径 |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 请求日志保留天数，默认 `90` |
| `CB_GATEWAY_MAX_BODY_BYTES` | 单个 JSON 请求体上限，默认 `10485760` |
| `CB_GATEWAY_CORS_ORIGINS` | 允许的浏览器来源，逗号分隔，默认仅本机来源 |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | 设为 `1` 时允许未创建 API Key 的业务请求，默认关闭 |
| `CB_GATEWAY_SECURE_COOKIE` | 设为 `1` 时强制管理 Cookie 使用 Secure 标记 |
| `CB_GATEWAY_USER_AGENT` | 出站请求完整 User-Agent；默认官方 CLI 指纹 `CLI/2.109.2 CodeBuddy/2.109.2`，可设回 `codebuddy2openai/2.0` 回退历史 UA |
| `CB_GATEWAY_IDE_VERSION` | 官方指纹中的 CLI 版本号，默认 `2.109.2` |
| `CB_GATEWAY_STAINLESS_OS` | 官方指纹上报的操作系统，默认按当前平台推断 |
| `CB_GATEWAY_STAINLESS_PACKAGE_VERSION` | 官方 SDK 指纹版本号，默认 `5.10.1` |
| `CB_GATEWAY_NODE_VERSION` | 官方 SDK 指纹 Node 运行时版本，默认 `v22.13.1` |
| `CB_AUTH_DIR` | 指定 Work Buddy / CodeBuddy auth 文件目录 |
| `CB_HOST_AUTH_DIR` | Docker 启动脚本使用的宿主机 auth 目录 |
| `CB_CONTAINER_AUTH_DIR` | Docker 容器内 auth 挂载目录，默认 `/auth` |

## 官方请求头指纹

转发到上游的请求默认携带 Work Buddy / CodeBuddy 官方 CLI 客户端的完整请求头指纹（见 `fingerprint.py`），与官方客户端的出站请求保持一致：

- **通用头**：`X-Requested-With: XMLHttpRequest`、按账号域选择的 `Origin` / `Referer`（`www.codebuddy.cn` 或 `www.workbuddy.ai`）、`X-Product: SaaS`、`X-Domain`，以及 `X-Request-ID` / `X-B3-*` / `b3` 分布式追踪头。
- **Chat 头**：`Authorization: Bearer`、`X-User-Id`、`X-Enterprise-Id`、`X-Tenant-Id`、`X-IDE-Type/Name/Version`、`x-codebuddy-request: 1`、`X-Agent-Intent: craft`、每请求生成的 `X-Conversation-*`，以及官方 SDK 的 `x-stainless-*` 指纹头。
- **Billing 头**：余额 / 积分接口使用精简指纹（`Authorization`、`X-User-Id`、`X-Enterprise-Id`、`X-Tenant-Id`、`X-Domain`）。
- **Refresh 头**：`X-Refresh-Token` 只出现在 token 刷新接口，chat 请求绝不携带。

字段缺失时按官方 CLI 约定发送 `X-No-*` 标记（如 `X-No-User-Id: 1`），而不是空值。以上均可用 `CB_GATEWAY_USER_AGENT` 等环境变量覆盖（见上表）。指纹只作用于请求头；如上游进一步校验 TLS 指纹（JA3），需要额外部署支持 TLS 指纹模拟的转发层。

## 数据和安全

- 账号 Token 会在写入 SQLite 前加密；Windows 默认使用 DPAPI，其他平台使用主密钥或权限为 `0600` 的本地密钥文件。
- 旧数据库中的明文 Token 会在升级后的首次启动时自动迁移为加密格式；迁移或容器部署时应固定 `CB_GATEWAY_MASTER_KEY`。
- 请求日志默认保留 90 天，可通过 `CB_GATEWAY_LOG_RETENTION_DAYS` 调整。
- API Key 使用 SHA-256 哈希执行鉴权，完整 Key 另行加密保存，仅通过受 Admin 鉴权保护的管理接口返回。
- 从旧版本升级前已经创建的 Key 只有哈希，无法逆向恢复；这类 Key 仍可继续调用，但如需在管理页复制，应重新创建。
- 不要把数据库、auth 文件、`.lab-agent`、日志或截图发给别人。
- 不建议把服务监听到公网地址。
- 如果只是本机使用，保持默认 `127.0.0.1` 最安全。

## 文件结构

```text
buddy2api/
├── server.py           # FastAPI 主服务
├── proxy.py            # 请求代理转发
├── auth_manager.py     # Work Buddy / CodeBuddy 登录凭据管理
├── database.py         # SQLite 数据层
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
