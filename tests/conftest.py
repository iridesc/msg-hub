from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from msg_hub.service import Hub
from msg_hub.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cfg(tmp_path: Path) -> Settings:
    base = Settings()
    return dataclasses.replace(
        base,
        data_dir=tmp_path / "data",
        types_dir=REPO_ROOT / "types",
        token="",
        default_rate_limit=0,
    )


@pytest.fixture()
def hub(cfg: Settings) -> Hub:
    return Hub(cfg)


WECOM_SETTINGS: dict[str, Any] = {"webhook_key": "693a-test-key-abcdefg"}


@dataclass
class MockWecom:
    """本地假企业微信 webhook，验证真实发送链路（payload / 错误码映射）。"""

    base_url: str
    status: int = 200
    body: dict[str, Any] = field(default_factory=lambda: {"errcode": 0, "errmsg": "ok"})
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last(self) -> dict[str, Any]:
        assert self.requests, "没有收到任何请求"
        return self.requests[-1]


@pytest.fixture()
def mock_wecom():
    mock = MockWecom(base_url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            mock.requests.append({"path": self.path, "json": json.loads(self.rfile.read(length) or b"{}")})
            payload = json.dumps(mock.body).encode()
            self.send_response(mock.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # 静音
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mock.base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield mock
    finally:
        server.shutdown()
        server.server_close()
