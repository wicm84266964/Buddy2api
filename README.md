# Buddy 2 API

[English](README_EN.md) | 中文

> 将桌面端 AI 编程助手的订阅额度转换为标准 OpenAI 兼容 API，带 Web 管理界面。

## 这是什么？

如果你正在使用某款自带模型额度的 VS Code AI 编程插件（比如腾讯出品的那款），你会发现它的额度只能在插件内使用。Buddy 2 API 帮你把这些额度"解放"出来，变成标准的 OpenAI API 接口，这样你就可以在 OpenClaw、Cherry Studio、NextChat 等任意客户端中使用。

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

打开浏览器访问 `http://127.0.0.1:8787`

## 前提条件

1. 已安装桌面端 AI 编程助手（VS Code 插件形式）
2. 插件已登录账号，拥有可用的模型额度

启动时自动扫描本机 auth 文件导入账号。

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
| `--admin-token` | 自动生成 | 管理 API Token |
| `--no-admin-auth` | `false` | 关闭管理 API 鉴权 |

### 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理后台 Token |
| `CB_GATEWAY_DB_PATH` | SQLite 数据库路径 |
| `CB_AUTH_DIR` | 指定 auth 文件目录 |

## 文件结构

```
buddy2api/
├── server.py           # FastAPI 主服务
├── proxy.py            # 请求代理转发
├── auth_manager.py     # 多账号管理
├── database.py         # SQLite 数据层
├── web/index.html      # Vue 3 Web UI
├── Dockerfile
├── docker-compose.yml
├── start.bat / start.sh
└── README.md
```

## License

MIT
