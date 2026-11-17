# msg-hub

多渠道路由的**消息发送 MCP 服务**：维护一份「渠道」清单（企业微信群机器人 / 邮箱），通过 MCP 工具或 REST 接口增删改查渠道，并统一发送消息。

一句话概括：**给某个群/某个人发消息，只要说"发到运维告警群"**，不用记 webhook。

```
                     ┌──────────────── msg-hub ────────────────┐
pi-box / Claude /  → │ /mcp   MCP Streamable HTTP              │
curl / 定时任务     → │ /api   REST                             │
                     │                                         │
                     │ tools    工具契约（两个前端共用一份）      │
                     │ service  寻址 / 护栏 / 限流 / 审计       │
                     │ store    state.json（渠道 + token）      │
                     │ plugins  types/<type>/send_msg.py（子进程）│
                     └───────────────┬─────────────────────────┘
                                     │ 子进程 + stdin JSON
                     ┌───────────────┴──────────────┐
                     │ types/wecom_bot  → 企微群机器人 webhook
                     │ types/email      → SMTP
                     └──────────────────────────────┘
```

## 特性

- **渠道增删改查**：`channel_create` / `channel_update` / `channel_delete` / `channel_list` / `channel_get`
- **统一发送**：`send_message` 支持纯文本 / markdown，企微支持 `@成员`、`@所有人`（需渠道授权）
- **类型内省**：每种渠道类型带 `manifest.json` + JSON Schema，AI 建渠道前先查 schema，不会瞎填字段
- **进程隔离的插件模型**：一个类型 = 一个目录 = 一个 `send_msg.py`，崩了也不影响主程序
- **单一状态文件**：渠道、凭据、护栏、API Token 全在 `state.json`（`0600`）——一次原子写 = 不会出现「有渠道无配置」的烂数据，接口返回一律打码
- **防误发**：名称模糊匹配到多个时**报错并给候选**，绝不猜；`dry_run` 先校验；`@all` 需显式开启；单渠道限流
- **审计日志**：每条发送（含 dry_run、失败）写 `logs/YYYY-MM-DD.jsonl`，`message_logs` 可查
- **两种部署形态**：HTTP 服务（compose，供外部系统调用）/ **stdio 子进程（零依赖，直接塞进 pi-box 等宿主）**
- **自带 Dashboard**：HTTP 形态带一个单页看板，看渠道启用情况与最近发送记录，并能手工启停/增删渠道
- **零外部依赖**：核心只用标准库（无 pip 包），没有数据库、没有 MQ
- **工具契约只写一遍**：stdio 与 HTTP 两个前端共用 `msg_hub/tools.py` 里的同一份定义，不会各自漂移

## 两种部署形态

| 形态 | 入口 | 依赖 | 鉴权 | 适用 |
|---|---|---|---|---|
| **stdio（推荐集成 pi-box）** | `python3 src/msg_hub/stdio.py` | **仅 python3** | 不需要（宿主拉起，天然在信任边界内） | 由宿主（pi-box / Claude Desktop）作为子进程拉起 |
| HTTP 服务 | `python3 -m msg_hub`（compose） | `pip install .[http]` | **必须**配管理员密码，没配拒绝启动 | 多系统共用、用 curl / 定时任务调用 |

stdio 模式手写实现了 MCP 的 `initialize` / `tools/list` / `tools/call`，不依赖 mcp SDK，所以只要有 python3 就能跑（pi-box 镜像里就是 `python3 3.11`、无 pip）。数据目录默认 `~/.msg-hub`，可用 `MSG_HUB_DATA_DIR` 指定。

手工验证：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  | python3 src/msg_hub/stdio.py
```

## 快速开始（HTTP 服务）

```bash
cp .env.example .env
# 必填：MSG_HUB_ADMIN_PASSWORD=...（openssl rand -hex 24）
#   没填的话 HTTP 服务会**直接拒绝启动**（能发消息/删渠道的服务不允许裸奔）
# 国内构建慢可设 PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

podman compose up -d --build     # 或 docker compose up -d --build
curl -s localhost:8788/health | jq
```

启动时的鉴权自检：

```text
$ MSG_HUB_ADMIN_PASSWORD= python -m msg_hub
[msg-hub] 拒绝启动：鉴权未配置

[错误] HTTP 模式必须配置管理员密码 MSG_HUB_ADMIN_PASSWORD（Basic 认证用）。
        生成：openssl rand -hex 24
        ...
```

如果 `.env` 里写了但没导进环境变量，错误信息会直接指出这个坑：

```text
[错误] 检测到 /path/.env 里设置了 MSG_HUB_ADMIN_PASSWORD，但当前进程的环境变量里没有它——
       服务不会自动读 .env，请用 `set -a; . ./.env; set +a` 导出，或交给 compose / systemd 注入。
```

### 新建一个企业微信群渠道

1. 在企业微信群里「添加群机器人」，拿到 webhook 地址
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`
2. 创建渠道（REST）：

```bash
curl -s -X POST localhost:8788/api/channels \
  -u "admin:$MSG_HUB_ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "运维告警群",
        "type": "wecom_bot",
        "settings": {"webhook_key": "xxxxxxxx"},
        "options": {"allow_mention_all": true, "rate_limit_per_min": 30}
      }' | jq
```

3. 发消息：

```bash
curl -s -X POST localhost:8788/api/send \
  -u "admin:$MSG_HUB_ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"channel": "运维告警群", "text": "**磁盘占用 92%**", "format": "markdown"}' | jq
```

### 新建一个邮箱渠道

```bash
curl -s -X POST localhost:8788/api/channels \
  -u "admin:$MSG_HUB_ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "我的QQ邮箱",
        "type": "email",
        "settings": {
          "smtp_host": "smtp.qq.com",
          "smtp_port": 465,
          "security": "ssl",
          "username": "me@qq.com",
          "password": "QQ邮箱授权码，不是登录密码",
          "to": ["me@qq.com"],
          "subject_prefix": "[msg-hub] "
        }
      }' | jq
```

> Gmail 在国内直连大概率不通，可在 settings 里加 `"socks5_proxy": "socks5://PROXY_HOST:PORT"`。

## 接入 pi-box（stdio，推荐）

pi-box 的 MCP 是"子进程 + stdio"模型，所以 msg-hub 用 stdio 形态最省事：**不需要额外容器、不需要端口、不需要 token**。

### 1. 把源码只读挂载进 pi-box 容器

在 pi-box 的 `docker-compose.yml` 里加一行（`../msg-hub` 指向本仓库）：

```yaml
    volumes:
      - ./workspace:/workspace
      - ./agent-home:/home/agent
      - ../msg-hub:/opt/msg-hub:ro     # ← msg-hub 源码（只读）
```

### 2. 注册 MCP

界面「系统设置 → MCP → 新增」，或直接调 API：

```bash
curl -s -X POST http://127.0.0.1:8790/api/mcp \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "msg-hub",
    "config": {
      "command": "python3",
      "args": ["/opt/msg-hub/src/msg_hub/stdio.py"],
      "env": { "MSG_HUB_DATA_DIR": "/home/agent/.pi-boxd/msg-hub" },
      "description": "消息渠道中心：渠道增删改查 + 统一发送（企微群 / 邮箱）",
      "exposure": "direct",
      "timeout": 60
    }
  }'

# 验证（走真实 stdio 握手）
curl -s -X POST http://127.0.0.1:8790/api/mcp/test -H 'Content-Type: application/json' -d '{"name":"msg-hub"}'
```

> 数据目录务必用绝对路径（`/home/agent/.pi-boxd/msg-hub`）：子进程的 cwd 是 `/workspace`，相对路径会写错位置。放在 `agent-home` 下可随现有备份策略持久化。

### 3. 给角色授权

在该助理/Agent 的 `.md` frontmatter 加 `mcps: [msg-hub]`（或界面里勾选）。

### 4. 用起来

跟助理说："把运维告警群加进来，webhook key 是 xxx"，然后"往运维告警群发条消息：磁盘告警"。

> ⚠️ **容器代理坑**：podman 会把宿主机的 `http_proxy`（如 `127.0.0.1:11001`）注入容器，而容器内并不存在该代理。wecom 插件默认**忽略环境代理**，只有显式配置 `http_proxy` 时才走代理——所以无需额外处理。若确实要经代理访问企业微信，在渠道 `settings` 里填 `http_proxy`。

### HTTP 服务形态（可选）

如果还想让别的系统（bash、定时任务、其他平台）用 REST 调用，再额外跑 compose 版，并通过 MCP HTTP 传输接入：

```yaml
# msg-hub/compose.yaml
services:
  msg-hub:
    networks: [pi-box-net]
networks:
  pi-box-net:
    external: true
    name: pi-box_default
```

```json
{
  "mcpServers": {
    "msg-hub-http": {
      "url": "http://msg-hub:8788/mcp",
      "headers": { "Authorization": "Bearer ${MSG_HUB_API_TOKEN}" },
      "description": "消息渠道中心（HTTP）",
      "exposure": "direct"
    }
  }
}
```

## MCP 工具

| 工具 | 作用 |
|---|---|
| `channel_types` | 列出渠道类型 + 能力 + `settings_schema`（建渠道前先调） |
| `channel_list` | 列出渠道（id / 名称 / 类型 / 可用状态） |
| `channel_get` | 单个渠道详情（凭据打码） |
| `channel_create` | 新建渠道：`name` / `type` / `settings` / `options` |
| `channel_update` | 改名称/类型/配置/护栏 |
| `channel_delete` | 软删除（配置移入 `config/.trash/`） |
| `send_message` | 发送：`channel` / `text` / `format` / `title` / `mention_*` / `dry_run` |
| `channel_test` | 测试配置：默认 dry_run；也可对**未保存**的 settings 做试探 |
| `message_logs` | 查发送审计日志 |

`channel` 参数既可以是 id（`2`）也可以是名称（`运维告警群`）。

## REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（免鉴权） |
| GET | `/api/types` | 渠道类型与 schema |
| GET | `/api/stats` | 聚合：渠道状态 + 最近发送情况（dashboard 用） |
| GET | `/api/channels?include_deleted=1` | 渠道列表 |
| POST | `/api/channels` | 新建渠道 |
| GET | `/api/channels/{ref}` | 渠道详情 |
| PATCH | `/api/channels/{ref}` | 修改渠道 |
| DELETE | `/api/channels/{ref}` | 删除渠道 |
| POST | `/api/channels/{ref}/test` | 测试（`{"dry_run": true}`；带 `settings`+`type` 可试未保存的配置） |
| POST | `/api/send` | 发送 |
| GET | `/api/logs?channel=&limit=` | 审计日志（**仅管理员**） |
| GET | `/api/tokens` | token 列表（**仅管理员**，不含明文） |
| POST | `/api/tokens` | 生成 token（**仅管理员**，返回明文一次） |
| PATCH | `/api/tokens/{id}` | 改名 / 启用 / 禁用（**仅管理员**） |
| DELETE | `/api/tokens/{id}` | 删除（**仅管理员**） |

## 鉴权：两套凭据，用途分开

| 通道 | 凭据 | 配置位置 | 能干什么 |
|---|---|---|---|
| **管理面** | Basic `用户名 + 密码` | 环境变量，**强制必填** | 看板、渠道增删改、token 增删改禁、**看日志**、发消息 |
| **调用面** | API Token | `state.json` 的 `tokens[]`，**可选** | **只能发消息** |

管理员密码没配就拒绝启动；API Token 是可选的第二条通道，**不建 token 时管理员照常能发消息**，不会出现「无鉴权」状态。

权限矩阵：

| 路由 / MCP 工具 | 管理员（Basic） | API Token（Bearer） |
|---|:--:|:--:|
| `/health` | 免 | 免 |
| `/dashboard`、`/api/channels*`、`/api/tokens*`、`/api/logs`、`/api/stats`、`/api/types` | ✅ | ❌ 403 |
| `/api/send`、MCP `send_message` / `channel_list` / `channel_types` | ✅ | ✅ |
| MCP `channel_create/update/delete/test`、`message_logs` | ✅ | ❌ 403 |

**token 看不到日志**——日志（含「谁发的」）只有管理员能读。

```bash
# 管理员（浏览器弹框用同一对凭据）
curl -u "admin:$MSG_HUB_ADMIN_PASSWORD" localhost:8788/api/channels

# 外部系统：先生成一把 token（明文只显示一次）
curl -u "admin:$MSG_HUB_ADMIN_PASSWORD" -X POST localhost:8788/api/tokens \
  -H 'Content-Type: application/json' -d '{"name":"监控平台"}'
# → {"ok":true,"token":{"id":1,"prefix":"mh_252170","secret":"mh_252170…"}}

# 用它发消息
curl -X POST localhost:8788/api/send \
  -H "Authorization: Bearer $MSG_HUB_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"channel":"运维告警群","text":"磁盘 92%"}'
```

## Dashboard

HTTP 服务形态附带一个单页 dashboard：`http://127.0.0.1:8788/dashboard`（根路径 `/` 会跳过去）。

<p align="center"><img src="docs/dashboard.png" alt="msg-hub dashboard" width="720"></p>

它能看到并操作：

- **渠道启用情况**：每个渠道的状态点（可用 / 已停用 / 损坏）、启用开关、删除按钮；顶部统计总数/可用/停用/损坏
- **发送情况**：最近 500 条发送记录（时间 / **调用者** / 来源 / 渠道 / 格式 / 成功·失败·dry_run / 错误码 / 内容 / 耗时）、成功率、按渠道分布
- **手工操作**：启用/停用渠道、重命名、删除（软删除）、**按类型 schema 动态渲染表单添加渠道**（`secret` 字段用密码框、枚举用下拉、默认值预填）
- **API Token 管理**（顶部「API Token」页签）：生成（明文弹窗显示一次）、启用/禁用（立即生效）、重命名、删除；能看到前缀、最后使用时间和累计次数

实现上没有任何新依赖：一个 HTML 文件（内联 CSS/JS，无构建、无 CDN），所有写操作走的都是已有的 `/api/channels` 接口 —— 和 MCP、REST 完全等价。页面本身不含任何凭据，接口返回的 secret 一律是打码后的值。

- 不想要就关掉：`MSG_HUB_DASHBOARD=0`
- dashboard 能删渠道、改凭据、生成 token，所以整个页面只对管理员开放（API Token 打不开）

## 数据布局

```text
data/
├── state.json            # 全部状态：渠道（含凭据）+ API Token，权限 0600
├── logs/2026-11-04.jsonl # 发送审计（按天分文件）
├── .cache/               # 插件可用的缓存目录（如 access_token）
├── .lock                 # 状态写入的跨进程锁
└── README.md             # 数据目录自述（首次运行生成）
```

`state.json` 结构：

```jsonc
{
  "version": 2,
  "next_id": 3,
  "next_token_id": 2,
  "channels": [
    {"id": 1, "name": "运维告警群", "type": "wecom_bot", "enabled": true,
     "settings": {"webhook_key": "...", "base_url": "...", "timeout": 10},  // 凭据
     "options": {"allow_mention_all": true, "rate_limit_per_min": 30},      // 护栏
     "created_at": "...", "updated_at": "..."}
  ],
  "deleted": [ /* 完整对象，含 settings，可直接人工恢复 */ ],
  "tokens": [
    {"id": 1, "name": "监控平台", "secret": "mh_…", "enabled": true,
     "created_at": "...", "last_used_at": "...", "usage_count": 12}
  ]
}
```

> 从 v1（`channels.json` + `config/<id>.json`）升级时自动迁移：合并进 `state.json`，
> 旧文件留档为 `channels.json.v1.bak` 和 `config.migrated-<时间戳>/`（可删）。
> 旧的 `MSG_HUB_TOKEN` 会自动变成一把名为「旧版 MSG_HUB_TOKEN（已迁移，可删除）」的 API Token，
> 现有 MCP 客户端不会断。

`options` 是所有类型通用的护栏：

| 字段 | 默认 | 说明 |
|---|---|---|
| `allow_mention_all` | `false` | 允许 `@所有人` |
| `rate_limit_per_min` | `30` | 单渠道每分钟发送上限，`0` = 不限；`dry_run` 只校验不真发，不占配额。计数在进程内存里（单进程部署语义） |

## 插件契约（新增渠道类型只需加目录）

内置类型放在 `src/msg_hub/types/<type>/`（随包分发，`pip install` 后开箱即用）；
要放第三方类型，把目录指到别处即可：`MSG_HUB_TYPES_DIR=/opt/my-types`。

`src/msg_hub/types/<type>/manifest.json`：

```json
{
  "type": "wecom_bot",
  "name": "企业微信群机器人",
  "description": "……",
  "entry": "send_msg.py",
  "capabilities": {
    "formats": ["plain", "markdown"],
    "mention": true,
    "mention_all": true,
    "attachments": false
  },
  "settings_schema": {
    "type": "object",
    "required": ["webhook_key"],
    "properties": {
      "webhook_key": {"type": "string", "title": "Webhook Key", "secret": true}
    }
  }
}
```

- `settings_schema` 用 JSON Schema；`title` / `description` / `default` / `secret` 会被 `channel_types` 直接暴露给模型
- `secret: true` 的字段在 `channel_get` / `channel_list` 中自动打码：普通值打码为 `***` + 末 4 位；URL 形态（webhook 地址）只打码 key 参数、保留主机便于排障
- 目录名必须等于 `type`

`src/msg_hub/types/<type>/send_msg.py` 调用契约（**v2：配置走 stdin，不再传 `--config`**）：

```bash
echo '{"settings":{"webhook_key":"xxx"},"message":{"text":"hello"}}' \
  | python3 src/msg_hub/types/wecom_bot/send_msg.py [--dry-run]
```

```jsonc
// stdin（一行 JSON）
{
  "version": 2,
  "request_id": "a1b2c3",
  "channel": {"id": 1, "name": "运维告警群", "type": "wecom_bot"},
  "settings": {"webhook_key": "xxx"},        // ← 该类型的配置（凭据），v2 新增
  "message": {
    "text": "abc", "format": "plain", "title": null,
    "mention_users": [], "mention_mobiles": [], "mention_all": false
  },
  "options": {"allow_mention_all": true},
  "dry_run": false,
  "cache_dir": "/data/.cache"
}
```

```jsonc
// stdout（一行 JSON）
{"ok": true, "provider": "wecom_bot", "message_id": "…", "elapsed_ms": 231, "raw": {"errcode": 0}}
{"ok": false, "provider": "wecom_bot",
 "error": {"code": "send_failed", "message": "errcode=93000 …", "retryable": false}}
```

- 退出码：`0` 成功 / `1` 可重试失败 / `2` 不可重试失败
- 消息体与配置都走 **stdin JSON**（不是 argv）：避免转义、换行、超长、命令注入问题；
  配置也在 stdin 里，插件因此不碰任何文件（纯函数：stdin 进 → stdout 出），主程序也不用为每次发送写临时文件
- 超时由主程序控制（`MSG_HUB_PLUGIN_TIMEOUT`，默认 20s）
- 子进程只继承白名单内的环境变量（不含任何凭据、不含宿主代理）；要代理请在渠道 settings 里显式配
- 需要 token 缓存的类型（企微应用消息、飞书等）可读写 `cache_dir`

## 错误码

| code | HTTP | retryable | 含义 |
|---|---|---|---|
| `not_found` | 404 | 否 | 渠道不存在（附 `available` 候选） |
| `ambiguous` | 409 | 否 | 名称命中多个（附 `candidates`） |
| `invalid_argument` | 400 | 否 | 参数不合法 |
| `invalid_settings` | 400 | 否 | 配置不符合 schema（附 `fields`） |
| `type_unknown` | 400 | 否 | 渠道类型不存在 |
| `channel_disabled` | 409 | 否 | 渠道已停用 |
| `channel_broken` | 409 | 否 | 配置缺失/损坏 |
| `rate_limited` | 429 | 是 | 超出频率限制（附 `retry_after_seconds`） |
| `mention_all_denied` | 403 | 否 | 未开启 `@所有人` |
| `text_too_long` | 400 | 否 | 消息超过长度上限 |
| `send_failed` | 502 | 视情况 | 下游发送失败 |
| `plugin_timeout` | 504 | 是 | 插件超时 |
| `plugin_error` | 502 | 视情况 | 插件崩溃/输出非法 |
| `state_broken` | 500 | 否 | `state.json` 损坏 |
| `unauthorized` | 401 | 否 | 缺少或错误的凭据 |
| `forbidden` | 403 | 否 | 当前凭据无权执行该操作（如 token 想管理渠道/看日志） |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MSG_HUB_ADMIN_USER` | `admin` | Basic 认证的用户名 |
| `MSG_HUB_ADMIN_PASSWORD` | 空 | Basic 认证的密码；**HTTP 必填，不填直接拒绝启动**（stdio 模式不需要） |
| `MSG_HUB_DATA_DIR` | `~/.msg-hub` | 数据目录（compose 里设为 `/data`） |
| `MSG_HUB_TYPES_DIR` | 包内 `msg_hub/types` | 渠道类型插件目录；指向别处即可换掉内置类型 |
| `MSG_HUB_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,msg-hub:*,[::1]:*` | MCP 传输层 Host 白名单（DNS rebinding 防护），跨容器部署要加上容器名 |
| `MSG_HUB_PLUGIN_TIMEOUT` | `20` | 插件超时（秒） |
| `MSG_HUB_MAX_TEXT` | `20000` | 单条消息最大字符数 |
| `MSG_HUB_DEFAULT_RATE_LIMIT` | `30` | 渠道默认每分钟上限 |
| `MSG_HUB_LOG_LEVEL` | `INFO` | 日志级别 |
| `MSG_HUB_DASHBOARD` | `1` | 是否开放 dashboard（`/dashboard`）；它能启停/删除渠道 |

## 安全

- **HTTP 模式强制鉴权**：没配 `MSG_HUB_ADMIN_PASSWORD` 就拒绝启动（不是打个警告继续跑）；密码太短会给警告
- **凭据文件权限 0600**，索引/日志同样 0600；`data/` 已在 `.gitignore`
- 接口返回的凭据一律打码，审计日志只存消息前 100 字
- `@所有人` 默认禁止，需在渠道 `options.allow_mention_all` 显式开启
- 建议只在 127.0.0.1 或内网暴露；跨容器通过 compose 网络访问，不要开公网端口
- 审计日志可回答"这条消息到底发没发"；审计写失败不会把已发出的消息变成失败（否则调用方重试会发两遍）
- 插件子进程只继承白名单内的环境变量，任何凭据都不会泄给插件
- 索引写入有跨进程文件锁，stdio 与 HTTP 同时指向一个数据目录也不会丢更新
- 审计日志按天分文件但**不会自动清理**，含消息片段，注意保留策略（`rm data/logs/*.jsonl`）

## 开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,http]"
pytest -q                       # 65 个测试，无需联网
python -m msg_hub               # HTTP 服务（默认 0.0.0.0:8788）
python src/msg_hub/stdio.py     # stdio MCP（零依赖，直接可跑）
```

新增一个 MCP 工具时，只改 `src/msg_hub/tools.py` 一处：stdio 与 HTTP 两个前端会自动同步。

## License

MIT
