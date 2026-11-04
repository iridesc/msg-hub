# msg-hub

多渠道路由的**消息发送 MCP 服务**：维护一份「渠道」清单（企业微信群机器人 / 邮箱），通过 MCP 工具或 REST 接口增删改查渠道，并统一发送消息。

一句话概括：**给某个群/某个人发消息，只要说"发到运维告警群"**，不用记 webhook。

```
                     ┌──────────────── msg-hub ────────────────┐
pi-box / Claude /  → │ /mcp   MCP Streamable HTTP              │
curl / 定时任务     → │ /api   REST                             │
                     │                                         │
                     │ service  寻址 / 护栏 / 限流 / 审计       │
                     │ store    channels.json + config/<id>.json│
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
- **凭据与索引分离**：`channels.json` 只放 id / 名称 / 类型；凭据在 `config/<id>.json`（`0600`），接口返回一律打码
- **防误发**：名称模糊匹配到多个时**报错并给候选**，绝不猜；`dry_run` 先校验；`@all` 需显式开启；单渠道限流
- **审计日志**：每条发送（含 dry_run、失败）写 `logs/YYYY-MM-DD.jsonl`，`message_logs` 可查
- **零外部依赖**：Python + httpx + smtplib，没有数据库、没有 MQ

## 快速开始

```bash
cp .env.example .env
# 必填：MSG_HUB_TOKEN=...（openssl rand -hex 24）
# 国内构建慢可设 PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

podman compose up -d --build     # 或 docker compose up -d --build
curl -s localhost:8788/health | jq
```

### 新建一个企业微信群渠道

1. 在企业微信群里「添加群机器人」，拿到 webhook 地址
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`
2. 创建渠道（REST）：

```bash
curl -s -X POST localhost:8788/api/channels \
  -H "Authorization: Bearer $MSG_HUB_TOKEN" \
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
  -H "Authorization: Bearer $MSG_HUB_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"channel": "运维告警群", "text": "**磁盘占用 92%**", "format": "markdown"}' | jq
```

### 新建一个邮箱渠道

```bash
curl -s -X POST localhost:8788/api/channels \
  -H "Authorization: Bearer $MSG_HUB_TOKEN" \
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

## 接入 pi-box

msg-hub 是**独立容器**，pi-box 通过 MCP 的 HTTP 传输接入（不是 stdio 子进程）。

1. 让两个 compose 在同一个网络里（在 `compose.yaml` 里取消 `networks` 注释，或手动指定）：

```yaml
services:
  msg-hub:
    networks: [pi-box-net]

networks:
  pi-box-net:
    external: true
    name: pi-box_default      # 实际名字用 `podman network ls` 确认
```

2. 在 pi-box 的「系统设置 → MCP」里新增（或直接编辑 `agent-home/.pi/agent/mcp.json`）：

```json
{
  "mcpServers": {
    "msg-hub": {
      "url": "http://msg-hub:8788/mcp",
      "headers": { "Authorization": "Bearer ${MSG_HUB_TOKEN}" },
      "description": "消息渠道中心：渠道增删改查 + 统一发送（企微群 / 邮箱）",
      "exposure": "direct",
      "timeout": 60
    }
  }
}
```

> `headers` 里的 `${MSG_HUB_TOKEN}` 由 pi-boxd 进程环境变量展开，记得在 pi-box 的 compose 里也注入同名变量。
> 若两个容器不在同一网络，可把 msg-hub 端口映射到本机后填 `http://host.containers.internal:8788/mcp`。

3. 给助理/Agent 授权：在该角色的 `.md` frontmatter 加 `mcps: [msg-hub]`（界面里勾选同理）。

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
| GET | `/api/channels?include_deleted=1` | 渠道列表 |
| POST | `/api/channels` | 新建渠道 |
| GET | `/api/channels/{ref}` | 渠道详情 |
| PATCH | `/api/channels/{ref}` | 修改渠道 |
| DELETE | `/api/channels/{ref}` | 删除渠道 |
| POST | `/api/channels/{ref}/test` | 测试（`{"dry_run": true}`） |
| POST | `/api/send` | 发送 |
| GET | `/api/logs?channel=&limit=` | 审计日志 |

鉴权：除 `/health` 外都需要 `Authorization: Bearer $MSG_HUB_TOKEN`（`MSG_HUB_TOKEN` 为空时不鉴权，仅限本机调试）。

## 数据布局

```text
data/
├── channels.json          # 索引：{id, name, type, enabled, created_at, updated_at}
├── config/
│   ├── 1.json            # {"settings": {...}, "options": {...}}
│   ├── 2.json
│   └── .trash/           # 删除渠道的配置备份
├── logs/2026-11-04.jsonl # 发送审计
└── .cache/               # 插件可用的缓存目录（如 access_token）
```

`options` 是所有类型通用的护栏：

| 字段 | 默认 | 说明 |
|---|---|---|
| `allow_mention_all` | `false` | 允许 `@所有人` |
| `rate_limit_per_min` | `30` | 单渠道每分钟发送上限，`0` = 不限 |

## 插件契约（新增渠道类型只需加目录）

`types/<type>/manifest.json`：

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
- `secret: true` 的字段在 `channel_get` / `channel_list` 中自动打码为 `***` + 末 4 位
- 目录名必须等于 `type`

`types/<type>/send_msg.py` 调用契约：

```bash
python3 types/wecom_bot/send_msg.py --config data/config/1.json [--dry-run]
```

```jsonc
// stdin（一行 JSON）
{
  "version": 1,
  "request_id": "a1b2c3",
  "channel": {"id": 1, "name": "运维告警群", "type": "wecom_bot"},
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
- 消息体走 **stdin JSON**（不是 argv）：避免转义、换行、超长、命令注入问题
- 超时由主程序控制（`MSG_HUB_PLUGIN_TIMEOUT`，默认 20s）
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
| `send_failed` | 502 | 视情况 | 下游发送失败 |
| `plugin_timeout` | 504 | 是 | 插件超时 |
| `plugin_error` | 502 | 视情况 | 插件崩溃/输出非法 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MSG_HUB_TOKEN` | 空 | Bearer Token；**生产必填** |
| `MSG_HUB_DATA_DIR` | `/data`（容器）/`./data` | 数据目录 |
| `MSG_HUB_TYPES_DIR` | `/app/types` | 插件目录 |
| `MSG_HUB_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,msg-hub:*,[::1]:*` | MCP 传输层 Host 白名单（DNS rebinding 防护），跨容器部署要加上容器名 |
| `MSG_HUB_PLUGIN_TIMEOUT` | `20` | 插件超时（秒） |
| `MSG_HUB_MAX_TEXT` | `20000` | 单条消息最大字符数 |
| `MSG_HUB_DEFAULT_RATE_LIMIT` | `30` | 渠道默认每分钟上限 |
| `MSG_HUB_LOG_LEVEL` | `INFO` | 日志级别 |

## 安全

- **凭据文件权限 0600**，索引/日志同样 0600；`data/` 已在 `.gitignore`
- 接口返回的凭据一律打码，审计日志只存消息前 100 字
- `@所有人` 默认禁止，需在渠道 `options.allow_mention_all` 显式开启
- 建议只在 127.0.0.1 或内网暴露；跨容器通过 compose 网络访问，不要开公网端口
- 审计日志可回答"这条消息到底发没发"

## 开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m msg_hub      # 本地起服务（默认 0.0.0.0:8788）
```

## License

MIT
