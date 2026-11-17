"""入口：python -m msg_hub"""

from __future__ import annotations

import sys

import uvicorn

from . import __version__
from .app import build_app
from .settings import settings


def main() -> int:
    settings.ensure_dirs()

    # 鉴权自检：令牌没配就拒绝启动（HTTP 服务能发消息/删渠道，不能裸奔）
    check = settings.check_auth()
    if not check.ok:
        print("[msg-hub] 拒绝启动：鉴权未配置\n", file=sys.stderr)
        print(check.render(), file=sys.stderr)
        return 1
    for warning in check.warnings:
        print(f"[msg-hub] 警告：{warning}", file=sys.stderr)

    app = build_app(settings)
    print(
        f"[msg-hub] v{__version__} 启动：http://{settings.host}:{settings.port}  "
        f"MCP=/mcp  REST=/api  Dashboard=/dashboard  数据目录={settings.data_dir}  "
        f"类型目录={settings.types_dir}  鉴权=Basic(管理员 {settings.admin_user}) + Bearer(API Token)",
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
