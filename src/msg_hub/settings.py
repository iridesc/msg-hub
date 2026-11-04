"""运行期配置：全部来自环境变量，容器内默认值可直接跑。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    version: str = __version__
    host: str = field(default_factory=lambda: _env("MSG_HUB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("MSG_HUB_PORT", 8788))
    data_dir: Path = field(default_factory=lambda: Path(_env("MSG_HUB_DATA_DIR", "./data")).resolve())
    types_dir: Path = field(
        default_factory=lambda: Path(
            _env("MSG_HUB_TYPES_DIR", str(Path(__file__).resolve().parents[2] / "types"))
        ).resolve()
    )
    token: str = field(default_factory=lambda: _env("MSG_HUB_TOKEN", ""))
    allowed_hosts: list[str] = field(
        default_factory=lambda: _env_list(
            "MSG_HUB_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,msg-hub:*,[::1]:*"
        )
    )
    plugin_timeout: float = field(default_factory=lambda: _env_float("MSG_HUB_PLUGIN_TIMEOUT", 20.0))
    max_text_chars: int = field(default_factory=lambda: _env_int("MSG_HUB_MAX_TEXT", 20000))
    default_rate_limit: int = field(default_factory=lambda: _env_int("MSG_HUB_DEFAULT_RATE_LIMIT", 30))
    log_level: str = field(default_factory=lambda: _env("MSG_HUB_LOG_LEVEL", "INFO").upper())

    @property
    def index_path(self) -> Path:
        return self.data_dir / "channels.json"

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def trash_dir(self) -> Path:
        return self.config_dir / ".trash"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / ".tmp"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / ".cache"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.token)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.config_dir, self.trash_dir, self.tmp_dir, self.log_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
