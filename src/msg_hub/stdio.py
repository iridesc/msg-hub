#!/usr/bin/env python3
"""msg-hub 的 stdio MCP 入口（零第三方依赖）。

用法（pi-box / Claude Desktop 等通过 stdio 拉起）：

    python3 /opt/msg-hub/src/msg_hub/stdio.py

可直接执行源码树（无需 pip install）：脚本会把 src/ 加进 sys.path。
数据目录默认 ~/.msg-hub，可用 MSG_HUB_DATA_DIR 覆盖（pi-box 里建议指向
/home/agent/.pi-boxd/msg-hub，随 agent-home 持久化）。

实现说明：MCP stdio 传输 = 换行分隔的 JSON-RPC 2.0。只实现 pi-mcp 等客户端
会用到的方法：initialize / notifications/initialized / ping / tools/list / tools/call。
不依赖官方 mcp SDK，使其能在仅有 python3 的环境（如 pi-box 容器）中运行。

工具契约（名称 / 描述 / 参数 / 处理函数）在 msg_hub.tools，与 HTTP 前端共用一份，
本文件只负责 JSON-RPC 传输。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

# 允许直接执行源码树文件（pi-box 挂载场景），把 src/ 加进模块搜索路径
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from msg_hub import __version__  # noqa: E402
from msg_hub.models import HubError  # noqa: E402
from msg_hub.service import Hub  # noqa: E402
from msg_hub.settings import Settings  # noqa: E402
from msg_hub.tools import INSTRUCTIONS, TOOLS, ToolSpec, guard  # noqa: E402

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

SERVER_INFO = {"name": "msg-hub", "title": "消息渠道中心", "version": __version__}


def _log(message: str) -> None:
    print(f"[msg-hub] {message}", file=sys.stderr, flush=True)


def _prepare(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """补默认值、丢掉未知参数。

    未知参数直接忽略（与 HTTP 侧 SDK 的行为一致），但记一条 stderr 日志，
    便于排查模型把参数名写错的情况。
    """
    known = {param.name for param in spec.params}
    unknown = [key for key in arguments if key not in known]
    if unknown:
        _log(f"工具 {spec.name} 收到未知参数，已忽略：{unknown}")
    prepared = {key: value for key, value in arguments.items() if key in known}
    for param in spec.params:
        if param.name not in prepared and not param.required:
            prepared[param.name] = param.default
    return prepared


# ── JSON-RPC / MCP 协议层 ─────────────────────────────────
class StdioServer:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.tools = {spec.name: spec for spec in TOOLS}
        self.initialized = False

    # 输入输出
    def _send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(
        self, request_id: Any, code: int, message: str, data: Any = None, **extra: Any
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        if extra:  # 容忍误传关键字参数，避免响应发不出去导致客户端假死
            error.setdefault("data", {}).update(extra)
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    # 方法分发
    def handle(self, message: dict[str, Any]) -> None:
        if "id" not in message:
            return  # notification（notifications/initialized、cancelled 等）直接忽略
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            self._result(request_id, self._initialize(params))
        elif method == "ping":
            self._result(request_id, {})
        elif method == "tools/list":
            self._result(
                request_id,
                {
                    "tools": [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "inputSchema": spec.input_schema(),
                        }
                        for spec in self.tools.values()
                    ]
                },
            )
        elif method == "tools/call":
            self._call_tool(request_id, params)
        elif method in ("resources/list", "prompts/list"):
            # 未实现的能力，按协议返回空列表
            self._result(request_id, {"resources" if method == "resources/list" else "prompts": []})
        else:
            self._error(request_id, -32601, f"未知方法：{method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or LATEST_PROTOCOL_VERSION)
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "")
        spec = self.tools.get(name)
        if spec is None:
            self._error(request_id, -32602, f"未知工具：{name}", {"available": sorted(self.tools)})
            return
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            self._error(request_id, -32602, "arguments 必须是对象")
            return
        arguments = _prepare(spec, arguments)
        try:
            guard(spec)
            result = spec.handler(self.hub, **arguments)
        except TypeError as exc:
            # 参数不匹配：缺必填 / 类型错误（注意 _error 的第 4 参是 data）
            self._error(request_id, -32602, f"参数不合法：{exc}", {"tool": name})
            return
        except HubError as exc:
            # 业务错误：作为工具结果（isError=true）返回，模型可读到结构化错误
            error = exc.to_dict()["error"]
            self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}],
                    "structuredContent": {"error": error},
                    "isError": True,
                },
            )
            return
        except Exception as exc:  # 未预期异常：兜底，避免整个 server 崩掉
            _log(f"工具 {name} 执行异常：{exc}\n{traceback.format_exc()}")
            self._result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps({"code": "internal_error", "message": str(exc)}, ensure_ascii=False)}
                    ],
                    "isError": True,
                },
            )
            return
        self._result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": {"result": result},
                "isError": False,
            },
        )

    def run(self) -> int:
        _log(f"stdio MCP server 就绪：tools={len(self.tools)} data={self.hub.settings.data_dir}")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _log(f"忽略非法 JSON 行：{line[:120]}")
                continue
            if not isinstance(message, dict):
                continue
            try:
                self.handle(message)
            except Exception as exc:  # pragma: no cover
                _log(f"处理消息异常：{exc}")
        return 0


def main() -> int:
    # stdio 模式由宿主进程（pi-box / Claude Desktop）拉起，天然在信任边界内，
    # 不需要令牌——HTTP 模式才强制要求（见 settings.check_auth）。
    settings = Settings()
    hub = Hub(settings)
    return StdioServer(hub).run()


if __name__ == "__main__":
    raise SystemExit(main())
