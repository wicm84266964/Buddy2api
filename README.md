# Buddy2api 2.0

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、Qoder、千问办公（QwenWork）、TraeWork、讯飞 Loomy、商汤小浣熊；管理页下拉选其中一个。一次请求只走一个通道。QClaw 已停止运营，通道已下架，积分请改用 WorkBuddy。

当前版本 **2.2.18**。默认通道含 WorkBuddy 国内/国际、Qoder 中国/国际、千问办公、TraeWork、Loomy、小浣熊。没有登录凭证的渠道不会出现在模型列表里。这个项目只供你本人持有的账号在本机使用。不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。

**仅限自己的账号。严禁使用接码平台、批量注册或任何批量方式薅羊毛。** 不要把网关公开给别人用，也不要代领或倒卖额度。

默认本机启动会自动打开管理页，无需填写管理 Token，重启后已有页面仍可直接使用。重复启动会打开已运行的同一实例；同一数据库不能同时由多个实例使用。后台服务可加 `--no-browser`。显式设置 `--admin-token` 或 `CB_GATEWAY_ADMIN_TOKEN` 时启用凭证管理模式；非本机监听必须设置该凭证，并在管理页设置中填写。客户端 API Key 和上游账号授权不受影响。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`。Key 可以绑一家通道，也可以选自动通道，用 `通道/模型` 指定这一跳的厂商。

默认通道都开。没装、没登录的通道，账号页检测为空，不会自动入库，模型列表也不会出现它们的模型名。QClaw 已下架，环境变量里的 `qclaw` 会被忽略。

Qoder 中国版读 `%APPDATA%\com.qodercn.app.stable\auth.v1.dat`，国际版读 `%APPDATA%\com.qoder.app.stable\auth.v1.dat`。两边的登录文件、模型目录和令牌不共用。额度在领取结果和官方余额里看。工具调用交回本机客户端。

讯飞 Loomy 读本机 `auth-session.json`。每日额度由上游自动发放，一键领取只核对到账余额；长对话发送前按 16 万 token 预算裁旧历史。设置页「上下文预算」可通过下拉框分别配置各渠道，选择「全选」可批量应用预算；填 0 关闭发送前裁剪。除 Loomy 外各渠道默认关闭，保存后对后续请求生效。商汤小浣熊读本机 `auth.json`，一键领取会触发每日发放。新版客户端在 `%USERPROFILE%\.office-raccoon\agent\config\auth.json`，旧版 `%USERPROFILE%\.box-agent\config\auth.json` 仍会扫描。文件里没有用户编号时，按登录 token 的用户标识分开保存；换号后再导入不会覆盖已经导入的另一个号。官方客户端如果在退出时作废旧登录，已保存的号仍会失效，需要在那个号还登录着的时候导入。两家都用自己的请求头，不走 WorkBuddy 的 CLI 指纹。

```powershell
python server.py
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy 国内 | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| WorkBuddy 国际 | 开 | 同上，域名是 `*.workbuddy.ai` 的账号会归到 `workbuddy-i` |
| Qoder 中国 | 开 | `%APPDATA%\com.qodercn.app.stable\auth.v1.dat` |
| Qoder 国际 | 开 | `%APPDATA%\com.qoder.app.stable\auth.v1.dat` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | `%APPDATA%\TRAE SOLO CN\User\globalStorage`，新装 Trae 也扫 `%APPDATA%\Trae CN\User\globalStorage` |
| 讯飞 Loomy | 开 | `%PUBLIC%\Loomy\*\userData\auth-session.json` |
| 商汤小浣熊 | 开 | `%USERPROFILE%\.office-raccoon\agent\config\auth.json`，旧版仍读 `%USERPROFILE%\.box-agent\config\auth.json` |

路径不对时可用 `CB_AUTH_DIR`、`CB_QODER_USER_DATA_DIR`、`CB_QODER_I_USER_DATA_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR`、`CB_LOOMY_AUTH_DIR`、`CB_XHX_AUTH_DIR` 指定。各通道的登录文件不要混在同一个目录。只要其中一家时，可设 `CB_GATEWAY_PROVIDERS=workbuddy` 收窄。

## 主动限流

在管理页「设置 → 主动限流」配置并保存。默认关闭，可先尝试每账号最小请求间隔 **5 秒**、最大并发 **1**，再按实际情况调整。

| 设置 | 默认 | 范围与作用 |
|---|---|---|
| 最小请求间隔 | 0 秒 | 0–600 秒；0 关闭间隔限制，按两次上游调用的开始时间计算 |
| 每账号最大并发 | 0 | 0–100；0 不限制，流式响应结束或取消才释放名额 |
| 最长排队时间 | 120 秒 | 1–600 秒；按账号先进先出，排队超时尝试队列中的其他账号 |

所有渠道的 Chat Completions 和 Responses、流式/非流式、内部重试和账号测试都受控。登录、签到、额度查询不占模型调用名额。设置保存后生效；间隔和并发都设为 0 即关闭。

全部候选账号排队超时后，返回 HTTP 429，Chat 错误码为 `local_rate_limit`，Responses 使用 `rate_limit_exceeded`；已开始的 SSE 则以错误事件结束。每账号最多排队 256 个请求，队列满时同样返回本地限流错误，不将账号标成失效。关闭跨渠道故障转移时，本地排队失败直接返回错误，不再遍历其他账号。

计数保存在当前网关进程内，重启后清空，独立网关进程不共享。降低速度不能保证不触发上游的 token、模型或累计用量限制。原有 `routing_account_rpm` 等诊断参数仍只用于统计，实际限流使用本节的新设置。

## 注意事项

按下面「安装与启动」即可。这几条是 2.0 里最容易踩空的：

1. **启动后账号页是空的，这是正常的。** 默认不再自动入库。到「账号」页：选通道 → 重新检测 → 一键导入。已启用的通道都能选。没有登录文件的通道不会出现在模型列表里。
2. **一把 API Key 只打一个通道。** 创建时默认是自动通道，只打一家时再改成具体通道。WorkBuddy 的 Key 发 `auto` / `glm-5.2`；Qoder 的 Key 发 `auto` 或 `Qwen3.8-Flash`；QwenWork 的 Key 发 `auto` 或 `qwork-advanced`；TraeWork 的 Key 发 `auto` 或 `qwen-3.7-plus`。通道和模型对不上会 400 或 403，不会帮你转到另一家。自动通道用 `qoder/DeepSeek-Flash` 这类带前缀的名字指定这一跳。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QwenWork 请在 Windows 上直接跑 `python server.py`。** Linux Docker 读不了它用 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
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

停止服务：回到终端按 `Ctrl+C`。一键更新后如果没有黑色窗口，点管理页右上角的「关闭」。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python server.py
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### Docker

官方镜像只发到 GitHub Container Registry，不发 Docker Hub。地址是 `ghcr.io/wicm84266964/buddy2api`，目前只有 `linux/amd64`。正式版发版成功后会推送同名标签，例如 `ghcr.io/wicm84266964/buddy2api:v2.2.18`，并移动 `latest`。预发布不移动 `latest`。请固定版本标签，不要默认追踪 `latest`。

第一次推送后，GitHub Packages 里的包可能是私有的。仓库所有者需要在包设置里把它改成公开，匿名 `docker pull` 才可用。这一步只做一次。

不要把端口暴露到公网。Compose 继续只绑定 `127.0.0.1:8787`。Linux 镜像读不了 Windows DPAPI。千问办公、Loomy、小浣熊请先用本机 `python server.py` 导入，再把数据目录挂给容器。

已克隆仓库、想用预构建镜像时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
docker compose pull
docker compose up -d
```

`docker-compose.yml` 钉住当前版本，并保留 `build: .`。想用仓库源码时，继续用下面的 Windows 脚本，或执行 `docker compose up -d --build`。

不克隆源码时：

```powershell
docker pull ghcr.io/wicm84266964/buddy2api:v2.2.18
docker run -d --name buddy2api --restart unless-stopped `
  -p 127.0.0.1:8787:8787 `
  -v buddy2api-data:/app/data `
  -e CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值" `
  -e CB_DOCKER=1 `
  ghcr.io/wicm84266964/buddy2api:v2.2.18
```

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\start.bat`。Linux / macOS：`chmod +x start.sh && ./start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`。
- **Docker：** 预构建镜像见上方「Docker」。`powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1` 仍在本机构建源码，不拉取 GHCR。本机没有 WorkBuddy 登录目录时脚本仍会启动。容器里通道都在，但千问办公、Loomy、小浣熊的本机登录文件请用上面的 `python server.py` 导入。导入后库里的 token 可以继续给 Docker 用。

### 第一次打开网页之后

本机管理页自动授权，不用粘贴 Token，也不依赖管理 Cookie。

1. 打开「账号」。WorkBuddy 可以点「浏览器登录」，在官方页面登录后自动入库，国内和国际站都能选。也可以点「一键导入全部通道」读取各启用通道的默认登录目录；自定义路径仅用于当前通道。未登录的通道会跳过。已入库的号用「一键续期并领取」续期并领取今日积分；只想领取时用「一键领取今日积分」。单个账号可在行菜单点「续期」。定时续期和定时领取在设置页，默认关闭，要手动打开并保存。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，创建时默认是自动通道，模型写成 `通道/模型`。只打一家时再改成具体通道。Codex 接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy、Loomy、小浣熊用 `auto`；Qoder 用 `auto` 或 `Qwen3.8-Flash`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`。上游加了新模型时，到「模型配置」点「一键读取供应模型」；各通道目录分开保存，选错通道仍会 400/403。Qoder 的额度在领取结果和官方余额里看。Loomy 的每日额度由上游自动发放，一键领取会读到账余额。小浣熊的一键领取会触发每日发放。

账号页的「保存全部修改」会保存所有已编辑账号（包括被筛选隐藏的行）；失败的账号保留未保存状态，可以重试。单条保存也会保留其他行的编辑。

自动通道 Key 发送裸别名或 `auto` 时，会在支持该模型的通道中，选择启用账号最高优先级最大的通道；优先级数字越大越先用，同级保持默认通道顺序。绑定通道的 Key 和 `通道/模型` 仍固定使用指定通道。可重试错误会遍历该通道的账号池，不再限于三个账号；不可重试错误和已经开始输出的流式响应仍遵守各通道的停止规则。请求日志显示「别名 → 通道 / 真实模型」，也可按真实模型搜索；旧日志不补填映射。

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python server.py
```

### 更新

管理页打开后会在后台核对远端版本（优先走 jsDelivr，不挡页面）。有新版本时顶栏出现「更新到 vx.y.z」，点一下会 `git pull --ff-only`、安装依赖并重启。这次拉取和安装会沿用已有的 `HTTPS_PROXY`、`ALL_PROXY`，或系统代理里的明确地址；不写死端口，也不改 Git 配置。只填了自动配置脚本（PAC）、没有具体地址时，Git 仍会直连。查不到远端版本时不会报错。Docker、不是 git 克隆、或不在 `main` 上时，按钮会说明原因，仍可用下面的手动步骤。Docker 安装请拉取 compose 钉住的版本后重启：`docker compose pull`，然后 `docker compose up -d`。本机已是最新、只想看按钮时：`$env:CB_GATEWAY_FAKE_LATEST="9.9.9"` 再启动，点下去不会真的拉取或重启。

先 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r requirements.txt
python server.py
```

## 常见问题

- WorkBuddy 聚合响应（包括默认停转重试所用的工具续聊路径）在缺少完成标记时返回上游错误，不再把部分正文默认为正常 `stop`。明确的 `finish_reason` 后直接 EOF 仍被接受；仅收到 `[DONE]`、但正文没有结束原因时不会当作正常完成。此校验不能判定模型主动 `stop` 是否过早，也不保证解决所有长会话停转。

- `git` 或 `conda` 不是内部命令：关掉终端重开；Conda 用户改用 Miniconda Prompt。
- `No module named ...`：先 `conda activate buddy2api`，再 `python -m pip install -r requirements.txt`。
- 下载依赖很慢：确认能访问 PyPI，不要混用好几个 Python。
- 端口 8787 被占用：关掉旧的 Buddy2api，或 `python server.py --port 8788`。
- 网页里一个账号都没有：还没导入。选对通道再检测；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`。
- 创建 Key 失败：没选通道。
- 客户端 503 `channel_unavailable`：这个模型当前一个可用账号都没有（账号全在冷却、过期或额度耗尽）。这个请求不计入 Key 的日限额。
- 客户端 400 `model_not_allowed`：模型不在这把 Key 的可访问范围内（白名单里没有它，或名字带的通道和 Key 不一致）。
- 客户端 400 `unknown_model`：这个名字没有任何通道的模型表认识它。
- 客户端 400 `channel_retired`：名字里带的通道已下线（如 `qclaw/x`）。
- 请求的模型 id 以 `/v1/models` 为准。带通道前缀（如 `qoder/DeepSeek-Flash`）或绑定通道的 Key 只在该通道内换号；自动通道 Key 的裸 id 可跨通道回退，Chat 和 Responses 使用同一规则。优先级先决定账号档位，同档按通道顺序，再保留账号权重及当前账号偏好，不强制分流。到期时间和速率读数仅展示，不改变用户偏好。
- 不同通道的原生模型 ID、大小写、版本和后缀不会自动归一。需要跨通道调用同一模型时，在各通道配置同一个别名，分别指向对应的原生 ID；请核实它们确实属于你希望互为备用的模型。不要通过删除 `x`、日期或推理后缀猜测等价关系。带通道的白名单逐个候选检查。
- 实测总余额为 0 的账号只退出请求调度，保持 active，仍可签到和保活。签到积累后，在账号页点击「额度」刷新，读到正余额即恢复；单纯切换启用状态不会清除旧的零余额。免费模型可按通道标记「忽略余额检查」。余额查询失败不会被写成 0。
- Key 日限额在账号队列非空后、首次派发前计 1 次；内部换号和跨通道重试不重复计次，最终上游失败也不退次数。请求校验失败或空队列不计次。

## 从 1.4.x 升级

启动时会自动改数据库。旧 Key 视为绑在 `workbuddy` 上，原来的 `auto` / `glm-5.2` 还能用。

和 1.4 不同的地方：启动不再自动导入账号；空仓是 503 而不是普通 `server_error`；新建 Key 必须选通道；官方余额只显示积分，不把各厂数字加在一起。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 管理页创建，已绑定通道 |
| 模型 | WorkBuddy：`auto` / `glm-5.2`。Qoder：`auto` 或 `Qwen3.8-Flash`。QwenWork：`auto` 或 `qwork-advanced`。TraeWork：`auto` 或 `qwen-3.7-plus`。Loomy、小浣熊：`auto` |
| Stream | 建议开 |

接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`。绑死一家的 Key，没加前缀的 `auto` 走那一家。自动通道的 Key，带前缀的模型打到对应通道；`auto` 走第一个已有账号的通道。额度用尽不会换到另一家。

### 模型容量与自动发现

「一键读取供应模型」会保存上游提供的输入/输出容量。`/v1/models` 返回 `context_window` 和 `max_output_tokens`；WorkBuddy 的 `maxInputTokens` / `maxOutputTokens` 会映射为这两个字段，不把官方客户端的 `contextWindow.defaultLength` 当成模型最大容量。DSH 自定义提供方使用「获取可用模型」重新添加并保存模型时，可自动读取容量；已有模型配置不会自动覆盖。

各通道缺失的容量字段分别默认显示 262,144 上下文 / 32,768 输出，`capacity_source` 标记每个字段来自 `catalog` 还是 `fallback`。默认值是配置兜底，不是上游保证；同名模型在不同通道的限制也可能不同。仅当目录中有已知输出限制时，网关会把显式的 `max_tokens` / `max_completion_tokens` 裁剪到该限制（Responses 的 `max_output_tokens` 经转换后同样适用）。未指定输出预算的请求不注入预算，未知容量不用于强制裁剪。达到输出上限仍可能以 `length` 结束，容量发现不能保证长任务永不截断。

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

QwenWork、TraeWork、Loomy、小浣熊各用自己那把 Key，不要混用。原来绑 QClaw 的 Key 要改到 WorkBuddy。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 不设置 | 本机默认自动授权；远程监听必须配置管理凭证 |
| `--no-admin-auth` | 关 | 显式启用本机自动授权，仍校验请求来源；只允许回环监听 |
| `--no-browser` | 关 | 不自动打开浏览器，适合后台服务 |

## 环境变量

| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,workbuddy-i,qwenwork,qoder,qoder-i,traework,loomy,xiaohuanxiong`。`qclaw` 已下架，写了也会忽略。只想留一家时再改 |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动导入。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取和一键续期并领取的账号间隔，默认 `800` |
| `CB_GATEWAY_KEEPALIVE` | 设 `1` 强制打开定时续期，设 `0` 强制关闭。不设时跟随设置页，默认关闭 |
| `CB_GATEWAY_KEEPALIVE_HOUR` | 定时续期的本地小时，默认 `22`。只在该小时前 10 分钟执行 |
| `CB_GATEWAY_AUTO_CHECKIN` | 设 `1` 强制打开定时领取，设 `0` 强制关闭。不设时跟随设置页，默认关闭 |
| `CB_GATEWAY_AUTO_CHECKIN_HOUR` | 定时领取的本地小时，默认 `9` |
| `CB_GATEWAY_DEFAULT_REASONING_EFFORT` | WorkBuddy DeepSeek V4 Pro/Flash 的默认思考强度，支持 `low` / `high` / `max`，默认 `high`；设为 `off` 可关闭默认值。Responses 的 `reasoning.effort` 或 Chat Completions 的 `reasoning_effort` 会覆盖它 |
| `CB_AUTH_DIR` | WorkBuddy 登录目录 |
| `CB_QODER_USER_DATA_DIR` | Qoder 中国版用户数据目录，下面应有 `auth.v1.dat` |
| `CB_QODER_I_USER_DATA_DIR` | Qoder 国际版用户数据目录，下面应有 `auth.v1.dat` |
| `CB_QWENWORK_AUTH_DIR` | QwenWork 登录目录 |
| `CB_TRAEWORK_AUTH_DIR` | TraeWork `storage.json` 所在目录 |
| `CB_LOOMY_AUTH_DIR` | Loomy 数据根目录，下面应有 `*/userData/auth-session.json` |
| `CB_LOOMY_CONTEXT_BUDGET` | Loomy 发送前上下文预算，默认 `160000`。`0` 关闭发送前裁剪 |
| `CB_PRETRIM_CHANNELS` | 按通道设置发送前上下文预算，逗号分隔 `通道=token`，如 `workbuddy=150000`；`0` 表示该通道不裁。除 Loomy 沿用原预算外，其他通道（含 WorkBuddy 国内/国际）默认不预裁，需显式配置此项或全局兜底预算后启用 |
| `CB_PRETRIM_BUDGET` | 未在 `CB_PRETRIM_CHANNELS` 中列出的通道的兜底预算，默认 `0`（不裁）。上游按 token 计用量频率时很有用 |
| `CB_GATEWAY_MAX_BODY_BYTES` | 请求体上限，默认 `33554432`（32 MiB） |
| `CB_XHX_AUTH_DIR` | 小浣熊 `auth.json` 所在目录或文件。不设时扫描新版 `.office-raccoon\agent\config\auth.json` 和旧版 `.box-agent\config\auth.json` |
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
