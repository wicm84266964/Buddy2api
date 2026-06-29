# Buddy2api

[English](README_EN.md) | 中文

> 把本机已经登录的腾讯 Work Buddy / CodeBuddy 账号接成 OpenAI 兼容 API，方便在 OpenCode、OpenClaw、Cherry Studio、NextChat 等工具里使用。

## 这是什么？

Work Buddy 2 API 是一个本地网关。它会扫描本机 Work Buddy / CodeBuddy 桌面端或插件保存的登录凭据，把请求转发到腾讯的模型接口，并在本机提供标准 OpenAI 兼容接口。

简单说：你已经在 Work Buddy 里登录并且有可用额度，这个项目把这份额度通过 `http://127.0.0.1:8787/v1` 暴露出来，让其他支持 OpenAI API 的客户端也能调用。

这个项目主要用于本地自用和测试。不要公开部署，不要共享给别人用，也不要把自己的登录凭据、API Key 或数据库文件发出去。

## 功能

- **OpenAI 兼容接口**：支持 `/v1/chat/completions` 和 `/v1/models`
- **流式输出**：支持 SSE 流式响应，也支持非流式聚合响应
- **自动导入账号**：启动时扫描本机 Work Buddy / CodeBuddy 的 auth 文件
- **多账号路由**：支持多个账号，优先级高的账号先用；同优先级会尽量固定当前账号，失败后自动切换
- **账号状态诊断**：支持启用/禁用、权重、优先级、单账号测试和 token 刷新
- **官方真实余额**：账号页可读取 Work Buddy 官方资源余额、30 天内即将到期额度和额度包明细
- **余额快照估算**：官方余额读取失败时，可填写账号当前剩余额度作为本地备用估算
- **手动领取每日积分**：账号页支持单账号领取，也支持一键领取所有已启用账号的今日积分
- **Token 自动刷新**：登录 token 快过期时自动刷新并写回数据库
- **API Key 管理**：给 OpenCode、Cherry Studio 等客户端单独创建 key
- **Key 安全存储**：只保存 SHA-256 哈希，完整 key 只在创建时显示一次
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
3. 本项目和调用客户端最好都运行在同一台机器上。

Windows 默认扫描路径类似：

```text
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

如果你的 auth 文件在别的目录，可以用 `CB_AUTH_DIR` 指定。

## 快速开始

### Windows

双击：

```bat
start.bat
```

或手动启动：

```powershell
pip install fastapi "uvicorn[standard]" httpx
python server.py
```

打开 Web UI 后，本机浏览器会自动使用同源 HttpOnly Cookie 完成管理认证，通常不需要手动粘贴 Admin Token。

如果你要远程访问或遇到 Cookie 异常，可以固定一个管理 token 作为备用：

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

如果只是启动服务，不自动挂载 Windows 登录目录，也可以用：

```bash
docker-compose up -d
```

启动后访问：

```text
http://127.0.0.1:8787
```

注意：Docker 容器不能凭空扫描 Windows 的 C 盘，必须通过 volume 挂载。脚本做的就是自动找路径并挂载，挂载方式是只读的。

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

当前只实现 Chat Completions 兼容接口：

```text
/v1/chat/completions
/v1/models
```

如果客户端有接口类型选项，请选择 **OpenAI Compatible / Chat Completions**。暂不支持固定调用 `/v1/responses` 的 Responses API 模式。

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
| `CB_AUTH_DIR` | 指定 Work Buddy / CodeBuddy auth 文件目录 |
| `CB_HOST_AUTH_DIR` | Docker 启动脚本使用的宿主机 auth 目录 |
| `CB_CONTAINER_AUTH_DIR` | Docker 容器内 auth 挂载目录，默认 `/auth` |

## 数据和安全

- `codebuddy_gateway.db` 会保存导入的账号凭据和请求日志。
- API Key 只保存哈希，创建后完整 key 不会再次显示。
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
