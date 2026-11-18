"""Dashboard：入口 / 路由 / 出口 的单页视图。

设计原则：
- **零新增依赖**：一个 HTML 文件（内联 CSS/JS），无构建步骤、无 CDN、无框架
- **不新增业务语义**：前端只调用已有的 /api/* 接口；本模块负责出页面 + 一个聚合统计
- **三栏并列对应三层模型**：入口（entries）/ 路由（rules）/ 出口（channels），点行弹窗改、右上角 ＋ 新建

页面本身不含任何凭据：后端返回的 secret 字段一律是打码后的值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .service import Hub

WEB_DIR = Path(__file__).resolve().parent / "web"


def index_html() -> str:
    """读 dashboard 页面（随包分发）。"""
    path = WEB_DIR / "index.html"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 打包漏文件时的兜底
        return (
            "<!doctype html><meta charset='utf-8'><title>msg-hub</title>"
            "<h1>msg-hub</h1><p>dashboard 页面文件缺失（src/msg_hub/web/index.html）</p>"
        )


def build_dashboard_routes(hub: Hub, enabled: bool = True) -> list[Route]:
    """dashboard 路由。enabled=False 时不注册（对外只暴露 /api 与 /mcp）。"""

    async def index(_request: Request) -> Response:
        return RedirectResponse("/dashboard", status_code=307)

    async def dashboard(_request: Request) -> Response:
        return HTMLResponse(
            index_html(),
            headers={
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
            },
        )

    if not enabled:
        return []

    return [
        Route("/", index, methods=["GET"]),
        Route("/dashboard", dashboard, methods=["GET"]),
    ]


def summarize_channels(hub: Hub) -> dict[str, Any]:
    """渠道状态汇总。"""
    channels = hub.list_channels()
    return {
        "total": len(channels),
        "ok": sum(1 for item in channels if item["status"] == "ok"),
        "disabled": sum(1 for item in channels if item["status"] == "disabled"),
        "broken": sum(1 for item in channels if item["status"] == "broken"),
    }


def summarize_sends(hub: Hub, limit: int = 500) -> dict[str, Any]:
    """最近发送情况汇总（基于审计日志）。"""
    records = hub.audit.query(limit=limit)
    by_channel: dict[Any, dict[str, Any]] = {}
    for record in records:
        info = record.get("channel") or {}
        channel_name = info.get("name")
        item = by_channel.setdefault(
            channel_name,
            {"name": channel_name or "(未知)", "ok": 0, "failed": 0},
        )
        if record.get("ok"):
            item["ok"] += 1
        else:
            item["failed"] += 1
    succeeded = sum(1 for record in records if record.get("ok"))
    return {
        "sampled": len(records),
        "ok": succeeded,
        "failed": len(records) - succeeded,
        "last_at": records[0].get("ts") if records else None,
        "by_channel": sorted(by_channel.values(), key=lambda item: -(item["ok"] + item["failed"])),
    }
