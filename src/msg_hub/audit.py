"""发送审计日志：按天写 jsonl，一行一条，便于 grep / jq。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import Settings


class Audit:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        settings.log_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_today(self) -> Path:
        return self.settings.log_dir / f"{datetime.now().astimezone():%Y-%m-%d}.jsonl"

    def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            path = self._path_for_today()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def query(self, channel_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        files = sorted(self.settings.log_dir.glob("*.jsonl"), reverse=True)
        collected: list[dict[str, Any]] = []
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if channel_id is not None:
                    channel = record.get("channel") or {}
                    if channel.get("id") != channel_id:
                        continue
                collected.append(record)
                if len(collected) >= limit:
                    return collected
        return collected
