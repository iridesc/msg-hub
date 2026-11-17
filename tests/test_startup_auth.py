"""HTTP 启动前的鉴权自检：令牌没配就拒绝启动。"""

from __future__ import annotations

import dataclasses


from msg_hub.__main__ import main
from msg_hub.settings import MIN_TOKEN_LENGTH, Settings

GOOD_TOKEN = "0123456789abcdef0123456789abcdef"


def _settings(password: str, **kwargs) -> Settings:
    base = Settings()
    return dataclasses.replace(base, admin_password=password, **kwargs)


def test_missing_token_is_an_error():
    check = _settings("").check_auth()
    assert check.ok is False
    assert any("MSG_HUB_ADMIN_PASSWORD" in item for item in check.errors)


def test_blank_token_counts_as_missing():
    check = _settings("   ").check_auth()
    assert check.ok is False


def test_valid_token_passes():
    check = _settings(GOOD_TOKEN).check_auth()
    assert check.ok is True
    assert check.warnings == ()


def test_short_token_warns_but_does_not_block():
    check = _settings("short").check_auth()
    assert check.ok is True
    assert check.warnings and str(MIN_TOKEN_LENGTH) in check.warnings[0]


def test_dotenv_with_empty_token_is_diagnosed(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MSG_HUB_ADMIN_PASSWORD=\nOTHER=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    check = _settings("").check_auth()
    assert check.ok is False
    assert any("空值" in item for item in check.errors)


def test_dotenv_with_token_but_not_exported_is_diagnosed(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MSG_HUB_ADMIN_PASSWORD=abcdef\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    check = _settings("").check_auth()
    assert check.ok is False
    assert any("不会自动读 .env" in item for item in check.errors)


def test_dotenv_without_token_gives_no_specific_hint(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    check = _settings("").check_auth()
    assert check.ok is False
    assert not any("空值" in item or "不会自动读" in item for item in check.errors)


def test_main_refuses_to_start_without_token(monkeypatch, tmp_path, capsys):
    """没有令牌时 main() 直接返回 1，绝不启动 HTTP 服务。"""
    monkeypatch.setattr(
        "msg_hub.__main__.settings",
        dataclasses.replace(Settings(), admin_password="", data_dir=tmp_path / "data"),
    )

    def boom(*args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("鉴权未配置时不应该启动 uvicorn")

    monkeypatch.setattr("msg_hub.__main__.uvicorn.run", boom)
    assert main() == 1
    captured = capsys.readouterr()
    assert "拒绝启动" in captured.err
    assert "MSG_HUB_ADMIN_PASSWORD" in captured.err


def test_main_starts_with_token(monkeypatch, tmp_path, capsys):
    started = {}

    def fake_run(app, **kwargs):  # pragma: no cover - 只验证被调用
        started["called"] = True

    monkeypatch.setattr(
        "msg_hub.__main__.settings",
        dataclasses.replace(Settings(), admin_password=GOOD_TOKEN, data_dir=tmp_path / "data"),
    )
    monkeypatch.setattr("msg_hub.__main__.uvicorn.run", fake_run)
    assert main() == 0
    assert started.get("called") is True
    assert "Basic(管理员" in capsys.readouterr().err


def test_stdio_mode_does_not_require_token():
    """stdio 模式不需要令牌（由宿主进程拉起，天然在信任边界内）。"""
    from msg_hub import stdio

    assert "check_auth" not in stdio.main.__code__.co_names
