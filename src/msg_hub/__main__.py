"""入口：python -m msg_hub"""

from __future__ import annotations

import sys

import uvicorn

from . import __version__
from .app import build_app
from .settings import settings


def main() -> int:
    settings.ensure_dirs()
    if not settings.auth_enabled:
        print("[msg-hub] 警告：未设置 MSG_HUB_TOKEN，/mcp 与 /api 处于无鉴权状态", file=sys.stderr)
    app = build_app(settings)
    print(
        f"[msg-hub] v{__version__} 启动：http://{settings.host}:{settings.port}  "
        f"MCP=/mcp  REST=/api  数据目录={settings.data_dir}  类型目录={settings.types_dir}  "
        f"鉴权={'Bearer' if settings.auth_enabled else '关闭'}",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
