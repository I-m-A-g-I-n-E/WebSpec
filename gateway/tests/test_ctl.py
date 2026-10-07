"""Tests for webspec-ctl CLI."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from webspec.ctl import main, build_parser


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Create temp ~/.claude.json and patch paths."""
    p = tmp_path / "claude.json"
    data = {
        "mcpServers": {
            "existing-svc": {
                "type": "http",
                "url": "http://localhost:9000/mcp",
                "guard": True,
            }
        }
    }
    p.write_text(json.dumps(data, indent=2))

    # Patch config_writer to use temp file
    monkeypatch.setattr("webspec.config_writer.default_config_path", lambda: p)
    monkeypatch.setattr("webspec.config_writer.default_env_path", lambda: tmp_path / ".env")
    (tmp_path / ".env").write_text("")

    # Patch caddy write to use temp dir
    conf_dir = tmp_path / "caddy"
    conf_dir.mkdir()
    monkeypatch.setattr("webspec.caddy.CADDY_CONF_DIR", conf_dir)
    monkeypatch.setattr("webspec.ctl.CADDY_PORT", 7001)
    monkeypatch.setattr("webspec.ctl.GATEWAY_PORT", 7002)
    monkeypatch.setattr("webspec.ctl.DOMAIN", "i-a-m.live")

    return p


# ── add ──


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_http_service(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "test-echo", "--type", "http", "--url", "https://echo.example.com/mcp", "--guard"])
    assert rc == 0

    data = json.loads(config_file.read_text())
    assert "test-echo" in data["mcpServers"]
    assert data["mcpServers"]["test-echo"]["url"] == "https://echo.example.com/mcp"
    assert data["mcpServers"]["test-echo"]["guard"] is True

    out = capsys.readouterr().out
    assert "test-echo" in out
    assert "ok" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_stdio_service(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "my-tool", "--type", "stdio", "--command", "python",
               "--args", "server.py", "--args", "my_module", "--no-guard"])
    assert rc == 0

    data = json.loads(config_file.read_text())
    assert "my-tool" in data["mcpServers"]
    assert data["mcpServers"]["my-tool"]["command"] == "python"
    assert data["mcpServers"]["my-tool"]["args"] == ["server.py", "my_module"]
    assert "guard" not in data["mcpServers"]["my-tool"]


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_duplicate_rejected(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_duplicate_with_force(mock_health, mock_wait, mock_reload, config_file):
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new-url", "--force"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    assert data["mcpServers"]["existing-svc"]["url"] == "http://new-url"


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_http_missing_url(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "no-url", "--type", "http"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--url required" in err


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_stdio_missing_command(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "no-cmd", "--type", "stdio"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--command required" in err


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_with_headers(mock_health, mock_wait, mock_reload, config_file):
    rc = main(["add", "auth-svc", "--type", "http", "--url", "http://x",
               "--header", "Authorization: Bearer ${TOKEN}",
               "--header", "X-Custom: value"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    headers = data["mcpServers"]["auth-svc"]["headers"]
    assert headers["Authorization"] == "Bearer ${TOKEN}"
    assert headers["X-Custom"] == "value"


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_with_secrets(mock_health, mock_wait, mock_reload, config_file, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("webspec.config_writer.default_env_path", lambda: env_file)
    rc = main(["add", "secret-svc", "--type", "http", "--url", "http://x",
               "--secret", "MY_API_KEY", "--secret", "MY_SECRET"])
    assert rc == 0
    content = env_file.read_text()
    assert "MY_API_KEY=" in content
    assert "MY_SECRET=" in content


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_name_normalization(mock_health, mock_wait, mock_reload, config_file, capsys):
    """Name that normalizes to empty string produces error."""
    rc = main(["add", "!!!", "--type", "http", "--url", "http://x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid service name" in err


# ── ls ──


@patch("webspec.ctl._health_check", return_value=False)
def test_ls_shows_services(mock_health, config_file, capsys):
    rc = main(["ls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "existing-svc" in out
    assert "http" in out
    assert "1 signed" in out  # guard: true → level 1


@patch("webspec.ctl._health_check", return_value=True)
def test_ls_empty(mock_health, tmp_path, monkeypatch, capsys):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr("webspec.config_writer.default_config_path", lambda: p)
    rc = main(["ls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No services" in out


# ── rm ──


@patch("webspec.ctl.reload_caddy", return_value=True)
def test_rm_service(mock_reload, config_file, capsys):
    rc = main(["rm", "existing-svc"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    assert "existing-svc" not in data["mcpServers"]
    out = capsys.readouterr().out
    assert "deprovisioned" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
def test_rm_with_clean_env(mock_reload, config_file, tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=1\nREMOVE_ME=secret\n")
    monkeypatch.setattr("webspec.config_writer.default_env_path", lambda: env_file)
    rc = main(["rm", "existing-svc", "--clean-env", "REMOVE_ME"])
    assert rc == 0
    content = env_file.read_text()
    assert "KEEP=1" in content
    assert "REMOVE_ME" not in content


# ── caddy-sync ──


@patch("webspec.ctl.reload_caddy", return_value=True)
def test_caddy_sync(mock_reload, config_file, tmp_path, monkeypatch, capsys):
    # ServiceRegistry() reads the config WEBSPEC_CONFIG names
    monkeypatch.setenv("WEBSPEC_CONFIG", str(config_file))
    conf_dir = tmp_path / "caddy"
    monkeypatch.setattr("webspec.caddy.CADDY_CONF_DIR", conf_dir)

    rc = main(["caddy-sync"])
    assert rc == 0
    assert (conf_dir / "existing-svc.caddy").exists()


@pytest.mark.parametrize("content", [None, "{not json", '{"mcpServers": {"x": {"type": "http"}}}'])
@patch("webspec.ctl.reload_caddy", return_value=True)
def test_caddy_sync_changes_nothing_when_the_config_is_rejected(mock_reload, config_file, tmp_path,
                                                                monkeypatch, capsys, content):
    # A missing file, malformed JSON, or valid JSON that is not a registry (an http entry
    # without url): the registry is empty, and a sync from it would remove every site block.
    monkeypatch.setenv("WEBSPEC_CONFIG", str(config_file))
    assert main(["caddy-sync"]) == 0
    block = tmp_path / "caddy" / "existing-svc.caddy"
    assert block.exists()
    mock_reload.reset_mock()
    if content is None:
        config_file.unlink()
    else:
        config_file.write_text(content)

    assert main(["caddy-sync"]) == 1
    assert block.exists()
    mock_reload.assert_not_called()
    assert "no Caddy config was changed" in capsys.readouterr().err


# ── parser ──


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_port_shorthand(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "my-app", "--port", "20128"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    svc = data["mcpServers"]["my-app"]
    assert svc["type"] == "http"
    assert svc["url"] == "http://localhost:20128"
    assert "guard" not in svc  # --port implies --no-guard


def test_parser_add_defaults():
    parser = build_parser()
    args = parser.parse_args(["add", "my-svc", "--type", "http", "--url", "http://x"])
    assert args.name == "my-svc"
    assert args.svc_type == "http"
    assert args.guard is None  # unset: cmd_add resolves it (new → guarded; --force → keep existing)


def test_parser_no_guard():
    parser = build_parser()
    args = parser.parse_args(["add", "my-svc", "--type", "http", "--url", "http://x", "--no-guard"])
    assert args.guard is False


# ── direct ──


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_direct_with_port(mock_health, mock_reload, config_file, tmp_path, monkeypatch, capsys):
    """--direct -p PORT creates Caddy-only proxy, no ~/.claude.json entry."""
    conf_dir = tmp_path / "caddy"
    monkeypatch.setattr("webspec.caddy.CADDY_CONF_DIR", conf_dir)

    rc = main(["add", "my-app", "--direct", "-p", "3000"])
    assert rc == 0

    # Should NOT be added to ~/.claude.json
    data = json.loads(config_file.read_text())
    assert "my-app" not in data["mcpServers"]

    # Should have Caddy config pointing directly at port 3000
    caddy_content = (conf_dir / "my-app.caddy").read_text()
    assert "reverse_proxy localhost:3000" in caddy_content
    assert "rate_limit" not in caddy_content

    out = capsys.readouterr().out
    assert "direct" in out.lower()
    assert "3000" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._health_check", return_value=False)
def test_add_direct_with_url(mock_health, mock_reload, config_file, tmp_path, monkeypatch, capsys):
    """--direct --url extracts port from URL."""
    conf_dir = tmp_path / "caddy"
    monkeypatch.setattr("webspec.caddy.CADDY_CONF_DIR", conf_dir)

    rc = main(["add", "web-ui", "--direct", "--url", "http://localhost:8080"])
    assert rc == 0

    caddy_content = (conf_dir / "web-ui.caddy").read_text()
    assert "reverse_proxy localhost:8080" in caddy_content

    data = json.loads(config_file.read_text())
    assert "web-ui" not in data["mcpServers"]


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_direct_requires_port_or_url(mock_health, mock_reload, config_file, capsys):
    """--direct without --port or --url fails."""
    rc = main(["add", "bad-svc", "--direct", "--type", "http"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--direct requires" in err


def test_parser_direct_flag():
    parser = build_parser()
    args = parser.parse_args(["add", "my-app", "--direct", "-p", "3000"])
    assert args.direct is True
    assert args.port == 3000


# ── https ──


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_https_shows_https_url(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "secure-svc", "--type", "http", "--url", "http://x", "--https"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://secure-svc.i-a-m.live" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_direct_https_shows_https_url(mock_health, mock_reload, config_file, tmp_path, monkeypatch, capsys):
    conf_dir = tmp_path / "caddy"
    monkeypatch.setattr("webspec.caddy.CADDY_CONF_DIR", conf_dir)
    rc = main(["add", "my-app", "--direct", "-p", "3000", "--https"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://my-app.i-a-m.live" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_without_https_shows_http_url(mock_health, mock_wait, mock_reload, config_file, capsys):
    rc = main(["add", "plain-svc", "--type", "http", "--url", "http://x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://plain-svc.i-a-m.live" in out


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_force_keeps_security_settings(mock_health, mock_wait, mock_reload, config_file, capsys):
    """--force must not silently drop level / tool overrides / labels."""
    import json as _json
    cfg = _json.loads(config_file.read_text())
    cfg["mcpServers"]["existing-svc"].update({"level": 4, "tools": {"x": {"tier": "dangerous"}}, "labels": ["eu"]})
    config_file.write_text(_json.dumps(cfg))
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new", "--force"])
    assert rc == 0
    entry = _json.loads(config_file.read_text())["mcpServers"]["existing-svc"]
    assert entry["level"] == 4 and entry["tools"] == {"x": {"tier": "dangerous"}} and entry["labels"] == ["eu"]
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new", "--force", "--level", "2"])
    assert _json.loads(config_file.read_text())["mcpServers"]["existing-svc"]["level"] == 2


@patch("webspec.ctl.reload_caddy", return_value=True)
@patch("webspec.ctl._wait_for_gateway", return_value=True)
@patch("webspec.ctl._health_check", return_value=True)
def test_add_force_keeps_guard_unless_given(mock_health, mock_wait, mock_reload, config_file):
    """Cloud review: --force without --no-guard silently escalated a level-0 service."""
    import json as _json
    assert main(["add", "local", "--type", "http", "--url", "http://a", "--no-guard", "--level", "0"]) == 0
    assert main(["add", "local", "--type", "http", "--url", "http://b", "--force"]) == 0
    entry = _json.loads(config_file.read_text())["mcpServers"]["local"]
    assert "guard" not in entry and entry["level"] == 0
    assert main(["add", "fresh", "--type", "http", "--url", "http://c"]) == 0
    assert _json.loads(config_file.read_text())["mcpServers"]["fresh"]["guard"] is True


@patch("webspec.ctl._health_check", return_value=False)
def test_ls_columns_align(mock_health, config_file, capsys):
    main(["ls"])
    header, _, row = capsys.readouterr().out.splitlines()[:3]
    assert header.index("HEALTH") == row.index("down")
