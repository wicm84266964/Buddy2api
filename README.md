# Buddy 2 API

> 将桌面端 AI 编程助手订阅转换为标准 OpenAI 兼容 API，带 Web 管理界面。

## 功能

- **OpenAI 兼容**：`/v1/chat/completions`（流式 SSE + 非流式）、`/v1/models`
- **多账号轮询**：自动扫描导入，最少使用优先负载均衡，故障自动切换
- **Token 自动刷新**：提前 60s 判定过期，自动调刷新接口并回写
- **API Key 管理**：创建/禁用/删除，模型权限控制，Key 仅保存 SHA-256 哈希
- **Web 管理界面**：Dashboard / 账号 / Key / 模型 / 日志 / 设置
- **Function Calling**：原生 `tools` / `tool_calls` 透传
- **请求日志**：Token 消耗、Credit、耗时、状态码
- **基础限额**：API Key 可设置每日请求次数上限
- **模型别名**：内置常用别名（gpt-4o、claude-3.5-sonnet 等），支持自定义扩展

## 快速开始

### Windows

```bat
双击 start.bat
```

或手动：

```powershell
pip install fastapi "uvicorn[standard]" httpx
python server.py
```

启动后控制台会打印本次 Admin Token。打开 Web UI 后点左下角「设置 Token」填入。

建议固定一个管理 token：

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

Docker 默认使用 `CB_GATEWAY_DB_PATH=/app/data/buddy2api.db`，数据库保存在宿主机 `./data/`。

打开浏览器访问 `http://127.0.0.1:8787`

## 前提条件

1. 已安装桌面端 AI 编程助手客户端
2. 桌面端已登录账号

启动时自动扫描本机 auth 文件导入账号。

## 启动参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址（`0.0.0.0` 允许外部访问） |
| `--port` | `8787` | 监听端口 |
| `--admin-token` | 自动生成 | 管理 API Token（环境变量 `CB_GATEWAY_ADMIN_TOKEN`） |
| `--no-admin-auth` | `false` | 关闭管理 API 鉴权，仅建议本机临时测试 |
| `--log-level` | `warning` | 日志级别（debug/info/warning/error） |

### 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理后台 Token |
| `CB_GATEWAY_DB_PATH` | SQLite 数据库路径 |
| `CB_AUTH_DIR` | 指定 auth 文件目录，适合 Docker 挂载 |

## 客户端接入

### OpenClaw / Cherry Studio / NextChat 等

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Web UI「API Keys」页面创建 |
| Model | `auto` / `glm-5.2` / `kimi-k2.7` / `deepseek-v4-pro` 等 |
| Stream | 建议开启 |

支持模型别名映射：`gpt-4o` → `glm-5.2`、`claude-3.5-sonnet` → `deepseek-v4-pro` 等。

### curl

```bash
# 非流式
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-xxxxx" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-xxxxx" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'

# 模型列表
curl http://127.0.0.1:8787/v1/models
```

API Key 完整值只在创建时返回一次，之后列表只显示前缀。请创建后立即填入客户端。

## API 文档

### OpenAI 兼容端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | 聊天补全（流式/非流式） |
| `/v1/models` | GET | 模型列表 |
| `/health` | GET | 健康检查 |

### 管理端点（需 Admin Token）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/admin/stats` | GET | 统计数据 |
| `/admin/accounts` | GET | 账号列表 |
| `/admin/accounts/scan` | POST | 扫描导入 |
| `/admin/accounts` | POST | 添加账号 |
| `/admin/accounts/{id}` | PUT | 更新账号 |
| `/admin/accounts/{id}` | DELETE | 删除账号 |
| `/admin/accounts/{id}/refresh` | POST | 刷新 Token |
| `/admin/api-keys` | GET | Key 列表 |
| `/admin/api-keys` | POST | 创建 Key |
| `/admin/api-keys/{id}` | PUT | 更新 Key |
| `/admin/api-keys/{id}` | DELETE | 删除 Key |
| `/admin/logs` | GET | 请求日志 |
| `/admin/settings` | GET/PUT | 系统设置 |
| `/admin/models` | GET/PUT | 模型配置 |
| `/admin/aliases` | GET/PUT | 模型别名 |

## 可用模型

| 模型 | 说明 |
|---|---|
| `auto` | 自动路由 |
| `glm-5.2` | 智谱 GLM-5.2 |
| `glm-5.1` | 智谱 GLM-5.1 |
| `glm-5v-turbo` | 智谱 GLM-5V Turbo（视觉） |
| `kimi-k2.7` | Kimi K2.7 |
| `kimi-k2.6` | Kimi K2.6 |
| `kimi-k2.5` | Kimi K2.5 |
| `deepseek-v4-pro` | DeepSeek V4 Pro |
| `deepseek-v4-flash` | DeepSeek V4 Flash |
| `minimax-m3-pay` | MiniMax M3 |
| `hy3-preview-agent` | HY3 Preview Agent |

## 文件结构

```
buddy2api/
├── server.py           # FastAPI 主服务
├── proxy.py            # 请求代理转发
├── auth_manager.py     # 多账号管理
├── database.py         # SQLite 数据层
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── start.bat           # Windows 启动脚本
├── start.sh            # Linux/macOS 启动脚本
├── .gitignore
├── .dockerignore
├── README.md
└── web/
    └── index.html      # Vue 3 Web UI
```

## 注意事项

- **仅监听 127.0.0.1**：默认不对外暴露，需外部访问时加 `--host 0.0.0.0`
- **管理后台默认鉴权**：未配置 `CB_GATEWAY_ADMIN_TOKEN` 时会生成临时 Token，重启后变化
- **创建 API Key 后代理端点会强制校验 Key**：未创建任何 Key 时 `/v1/chat/completions` 仍保持本机调试放行
- **Token 有效期**：约 338 天，自动刷新
- **额度按 Token 计**：usage 中 `credit` 字段为腾讯计费单位
- **不要设太小的 max_tokens**：reasoning 会占用配额导致 content 为空
- **多账号轮询**：导入多个账号后自动按最少使用优先分发

## License

MIT
