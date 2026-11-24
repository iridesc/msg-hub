"""运行期配置：全部来自环境变量，容器内默认值可直接跑。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

# 令牌最短建议长度：短于此值给警告（不阻断启动）
MIN_TOKEN_LENGTH = 16


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
class AuthCheck:
    """HTTP 模式启动前的鉴权自检结果。

    errors 非空 → 拒绝启动；warnings 只提示不阻断。
    """

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        return "\n".join(
            [f"[错误] {item}" for item in self.errors] + [f"[警告] {item}" for item in self.warnings]
        )


@dataclass(frozen=True)
class Settings:
    version: str = __version__
    host: str = field(default_factory=lambda: _env("MSG_HUB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("MSG_HUB_PORT", 8788))
    # 默认 ~/.msg-hub：stdio 子进程的 cwd 不可控（如 pi-box 的 /workspace），不能依赖相对路径
    data_dir: Path = field(
        default_factory=lambda: Path(_env("MSG_HUB_DATA_DIR", str(Path.home() / ".msg-hub"))).expanduser().resolve()
    )
    # 内置类型插件随包分发，默认指向包内 types/；部署方可用 MSG_HUB_TYPES_DIR
    # 指向自己的插件目录（放第三方类型）。源码树运行时同样落在 src/msg_hub/types。
    types_dir: Path = field(
        default_factory=lambda: Path(
            _env("MSG_HUB_TYPES_DIR", str(Path(__file__).resolve().parent / "types"))
        ).resolve()
    )
    # ── 管理面凭据（强制）───────────────────────────────
    # 与「调用面 API Token」彻底分开：管理员靠 Basic，外部系统靠 token。
    admin_user: str = field(default_factory=lambda: _env("MSG_HUB_ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _env("MSG_HUB_ADMIN_PASSWORD", ""))
    # 旧版单一令牌（当 Basic 密码 + Bearer 双用）。仅为迁移兼容读一次：
    # 首次启动会把它变成 state.json 里的一把 token，之后就不再使用。
    legacy_token: str = field(default_factory=lambda: _env("MSG_HUB_TOKEN", ""))
    allowed_hosts: list[str] = field(
        default_factory=lambda: _env_list(
            "MSG_HUB_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,msg-hub:*,[::1]:*"
        )
    )
    plugin_timeout: float = field(default_factory=lambda: _env_float("MSG_HUB_PLUGIN_TIMEOUT", 20.0))
    max_text_chars: int = field(default_factory=lambda: _env_int("MSG_HUB_MAX_TEXT", 20000))
    default_rate_limit: int = field(default_factory=lambda: _env_int("MSG_HUB_DEFAULT_RATE_LIMIT", 30))
    log_level: str = field(default_factory=lambda: _env("MSG_HUB_LOG_LEVEL", "INFO").upper())
    # dashboard（/dashboard）是否开放；它能启停/删除渠道，默认开，介意就设 0 关掉
    dashboard_enabled: bool = field(
        default_factory=lambda: _env("MSG_HUB_DASHBOARD", "1").strip().lower() not in ("0", "false", "no", "off")
    )

    @property
    def state_path(self) -> Path:
        """全部持久化状态（渠道 + token）都在这一个文件里。"""
        return self.data_dir / "state.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / ".cache"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password.strip())

    # ── 启动自检 ────────────────────────────────────────
    def check_auth(self) -> AuthCheck:
        """HTTP 模式启动前的鉴权自检。

        HTTP 服务能发消息、能删渠道、能生成 token，**裸奔是不允许的**：
        管理员密码没配就直接拒绝启动，而不是打个警告继续跑。

        注意：API Token 是调用面的可选通道，**不在启动检查范围内**——
        没建任何 token 时，管理员自己仍然能发消息，不会出现无鉴权状态。

        只在 HTTP 入口（python -m msg_hub）调用；stdio 模式由宿主进程拉起，
        天然在信任边界内，不需要凭据。
        """
        errors: list[str] = []
        warnings: list[str] = []
        password = (self.admin_password or "").strip()
        if not password:
            errors.append(
                "HTTP 模式必须配置管理员密码 MSG_HUB_ADMIN_PASSWORD（Basic 认证用）。\n"
                "        生成：openssl rand -hex 24\n"
                "        注入方式任选：写进 .env 让 compose 读 / export MSG_HUB_ADMIN_PASSWORD=... / "
                "systemd 的 EnvironmentFile\n"
                "        临时调试：MSG_HUB_ADMIN_PASSWORD=xxx python -m msg_hub"
            )
            hint = self._dotenv_hint("MSG_HUB_ADMIN_PASSWORD")
            if hint:
                errors.append(hint)
        elif len(password) < MIN_TOKEN_LENGTH:
            warnings.append(
                f"MSG_HUB_ADMIN_PASSWORD 只有 {len(password)} 位，建议至少 {MIN_TOKEN_LENGTH} 位（openssl rand -hex 24）"
            )
        return AuthCheck(tuple(errors), tuple(warnings))

    def _dotenv_hint(self, key_name: str) -> str:
        """凭据没生效时，最常见的原因是 .env 里写了但没导进来。

        这里只做诊断（不改行为）：服务不自动读 .env，避免与 compose/systemd 的注入方式打架。
        """
        path = Path.cwd() / ".env"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() != key_name:
                continue
            if value.strip():
                return (
                    f"检测到 {path} 里设置了 {key_name}，但当前进程的环境变量里没有它——"
                    "服务不会自动读 .env，请用 `set -a; . ./.env; set +a` 导出，"
                    "或交给 compose / systemd 注入。"
                )
            return f"检测到 {path} 里的 {key_name} 是空值，请先填上（openssl rand -hex 24）。"
        return ""

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.log_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
