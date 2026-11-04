"""stdio MCP 协议层测试：拉起真实子进程，模拟 MCP 客户端逐行收发 JSON-RPC。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STDIO_ENTRY = REPO_ROOT / "src" / "msg_hub" / "stdio.py"


class StdioClient:
    """最小 MCP stdio 测试客户端。

    两个关键点（否则测试会假死）：
    - 后台线程排空 stderr：管道缓冲区写满会把子进程卡在 write() 上
    - 每次读响应带超时：真出问题时立刻失败并带上服务端 stderr，而不是无限等待
    """

    TIMEOUT = 15.0

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(STDIO_ENTRY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put("")  # EOF 标记

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:
            self._stderr.append(line)

    def send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        try:
            line = self._lines.get(timeout=self.TIMEOUT)
        except queue.Empty:
            raise AssertionError(f"等待响应超时（{self.TIMEOUT}s）；服务端 stderr：\n{''.join(self._stderr[-20:])}")
        assert line, f"server 提前退出；stderr：\n{''.join(self._stderr[-20:])}"
        return json.loads(line)

    def request(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, **({"params": params} if params else {})})
        return self.recv()

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments}, request_id=99)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    return {
        **env,
        "MSG_HUB_DATA_DIR": str(tmp_path / "data"),
        "MSG_HUB_TYPES_DIR": str(REPO_ROOT / "types"),
    }


def test_handshake_and_tool_list(tmp_path):
    client = StdioClient(_env(tmp_path))
    try:
        init = client.request(
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert init["result"]["serverInfo"]["name"] == "msg-hub"

        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        client.request("ping")

        listed = client.request("tools/list")
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {
            "channel_types",
            "channel_list",
            "channel_get",
            "channel_create",
            "channel_update",
            "channel_delete",
            "send_message",
            "channel_test",
            "message_logs",
        }
        send_tool = next(t for t in listed["result"]["tools"] if t["name"] == "send_message")
        assert send_tool["inputSchema"]["required"] == ["channel", "text"]
    finally:
        client.close()


def test_protocol_version_falls_back(tmp_path):
    client = StdioClient(_env(tmp_path))
    try:
        init = client.request(
            "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
    finally:
        client.close()


def test_full_channel_lifecycle_over_stdio(tmp_path):
    client = StdioClient(_env(tmp_path))
    try:
        client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        created = client.call_tool(
            "channel_create",
            {"name": "工作群1", "type": "wecom_bot", "settings": {"webhook_key": "k-123456789"}},
        )["result"]
        assert created["isError"] is False
        channel = json.loads(created["content"][0]["text"])
        assert channel["id"] == 1
        assert channel["settings"]["webhook_key"] == "***6789"

        sent = client.call_tool("send_message", {"channel": "工作群1", "text": "hi", "dry_run": True})["result"]
        assert sent["isError"] is False
        assert json.loads(sent["content"][0]["text"])["ok"] is True

        logs = client.call_tool("message_logs", {"channel": 1, "limit": 5})["result"]
        assert json.loads(logs["content"][0]["text"])["logs"][0]["dry_run"] is True

        # 参数缺 name → JSON-RPC 错误（-32602），而不是崩溃
        bad = client.request("tools/call", {"name": "channel_create", "arguments": {"type": "email", "settings": {}}})
        assert bad["error"]["code"] == -32602

        # 未知工具
        unknown = client.request("tools/call", {"name": "nope", "arguments": {}})
        assert unknown["error"]["code"] == -32602

        # 业务错误：渠道不存在 → isError=true 的工具结果
        missing = client.call_tool("send_message", {"channel": "不存在", "text": "x"})["result"]
        assert missing["isError"] is True
        assert json.loads(missing["content"][0]["text"])["code"] == "not_found"
    finally:
        client.close()
