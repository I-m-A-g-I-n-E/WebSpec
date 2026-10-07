"""Tests for webspec-ctl CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from webspec import caddy, ctl
from webspec.caddy import read_site, Site
from webspec.ctl import main, build_parser
from tests.test_caddy_transaction import make_caddy_host


@pytest.fixture
def probes(monkeypatch):
    """The gateway and Caddy answer; tests change the answers through the namespace.

    ``health`` is what ctl._health_check finds: ctl.OK, ctl.GUARDED, or None for down."""
    answers = SimpleNamespace(health=ctl.OK, ready=True)
    monkeypatch.setattr(ctl, "_health_check", lambda *a, **kw: answers.health)
    monkeypatch.setattr(ctl, "_wait_for_gateway", lambda *a, **kw: answers.ready)
    return answers


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A migrated Linux host whose Caddy configuration records the public domain i-a-m.live."""
    return make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")


@pytest.fixture
def config_file(tmp_path, monkeypatch, host, probes):
    """Create temp ~/.claude.json and point webspec-ctl at it."""
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
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / ".env"))
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr("webspec.ctl.CADDY_PORT", 7001)
    monkeypatch.setattr("webspec.ctl.GATEWAY_PORT", 7002)
    return p


def _entries(config_file):
    return json.loads(config_file.read_text())["mcpServers"]


# ── add ──


def test_add_http_service(config_file, capsys):
    rc = main(["add", "test-echo", "--type", "http", "--url", "https://echo.example.com/mcp", "--guard"])
    assert rc == 0

    data = json.loads(config_file.read_text())
    assert "test-echo" in data["mcpServers"]
    assert data["mcpServers"]["test-echo"]["url"] == "https://echo.example.com/mcp"
    assert data["mcpServers"]["test-echo"]["guard"] is True

    out = capsys.readouterr().out
    assert "test-echo" in out
    assert "ok" in out
    assert f"Config:  {config_file}" in out


def test_add_stdio_service(config_file, capsys):
    rc = main(["add", "my-tool", "--type", "stdio", "--command", "python",
               "--args", "server.py", "--args", "my_module", "--no-guard"])
    assert rc == 0

    data = json.loads(config_file.read_text())
    assert "my-tool" in data["mcpServers"]
    assert data["mcpServers"]["my-tool"]["command"] == "python"
    assert data["mcpServers"]["my-tool"]["args"] == ["server.py", "my_module"]
    assert "guard" not in data["mcpServers"]["my-tool"]


def test_add_duplicate_rejected(config_file, capsys):
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err


def test_add_duplicate_with_force(config_file):
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new-url", "--force"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    assert data["mcpServers"]["existing-svc"]["url"] == "http://new-url"


def test_add_http_missing_url(config_file, capsys):
    rc = main(["add", "no-url", "--type", "http"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--url required" in err


def test_add_stdio_missing_command(config_file, capsys):
    rc = main(["add", "no-cmd", "--type", "stdio"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--command required" in err


def test_add_with_headers(config_file):
    rc = main(["add", "auth-svc", "--type", "http", "--url", "http://x",
               "--header", "Authorization: Bearer ${TOKEN}",
               "--header", "X-Custom: value"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    headers = data["mcpServers"]["auth-svc"]["headers"]
    assert headers["Authorization"] == "Bearer ${TOKEN}"
    assert headers["X-Custom"] == "value"


def test_add_with_secrets(config_file, tmp_path):
    env_file = tmp_path / ".env"
    rc = main(["add", "secret-svc", "--type", "http", "--url", "http://x",
               "--secret", "MY_API_KEY", "--secret", "MY_SECRET"])
    assert rc == 0
    content = env_file.read_text()
    assert "MY_API_KEY=" in content
    assert "MY_SECRET=" in content


def test_add_name_normalization(config_file, capsys):
    """Name that normalizes to empty string produces error."""
    rc = main(["add", "!!!", "--type", "http", "--url", "http://x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid service name" in err


def test_add_writes_and_loads_the_site_block(config_file, host):
    assert main(["add", "svc", "--type", "http", "--url", "http://x"]) == 0
    assert read_site(host.block("svc")) == Site("svc", "service", guard=True)
    assert [call.split()[0] for call in host.caddy.calls()] == ["validate", "reload"]


# ── ls ──


def test_ls_shows_services(config_file, probes, capsys):
    probes.health = None
    rc = main(["ls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "existing-svc" in out
    assert "http" in out
    assert "1 signed" in out  # guard: true → level 1


def test_ls_empty(tmp_path, monkeypatch, probes, capsys):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    rc = main(["ls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No services" in out


# ── rm ──


def test_rm_service(config_file, capsys):
    rc = main(["rm", "existing-svc"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    assert "existing-svc" not in data["mcpServers"]
    out = capsys.readouterr().out
    assert "deprovisioned" in out


def test_rm_with_clean_env(config_file, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=1\nREMOVE_ME=secret\n")
    rc = main(["rm", "existing-svc", "--clean-env", "REMOVE_ME"])
    assert rc == 0
    content = env_file.read_text()
    assert "KEEP=1" in content
    assert "REMOVE_ME" not in content


def test_rm_removes_the_site_block_and_reloads(config_file, host):
    assert main(["caddy-sync"]) == 0
    assert host.block("existing-svc").exists()
    calls = len(host.caddy.calls())
    assert main(["rm", "existing-svc"]) == 0
    assert not host.block("existing-svc").exists()
    assert [c.split()[0] for c in host.caddy.calls()[calls:]] == ["validate", "reload"]


# ── caddy-sync ──


def test_caddy_sync(config_file, host, capsys):
    rc = main(["caddy-sync"])
    assert rc == 0
    assert host.block("existing-svc").exists()
    assert "existing-svc" in capsys.readouterr().out
    assert [c.split()[0] for c in host.caddy.calls()] == ["validate", "reload"]


@pytest.mark.parametrize("content", [None, "{not json", '{"mcpServers": {"x": {"type": "http"}}}'])
def test_caddy_sync_changes_nothing_when_the_config_cannot_be_read(config_file, host, content, capsys):
    # sudo drops WEBSPEC_CONFIG; reading a missing or broken config as empty would delete every block.
    main(["caddy-sync"])
    before = host.snapshot()
    assert before
    calls = len(host.caddy.calls())
    if content is None:
        config_file.unlink()
    else:
        config_file.write_text(content)
    assert main(["caddy-sync"]) == 1
    assert host.snapshot() == before
    assert len(host.caddy.calls()) == calls
    err = capsys.readouterr().err
    assert "No site block was changed" in err
    # The hint fits the failure: a file that was read but is not a registry is the file's fault,
    # not a missing sudo (review of the deployment guide).
    if content is not None:
        assert "Fix the file, then run caddy-sync again" in err and "run this as root" not in err
    elif os.geteuid() != 0:
        assert "On a production host run this as root" in err
    else:
        assert "Check that the file exists and that root can read it" in err


def test_caddy_sync_with_no_services_removes_nothing(config_file, host, capsys):
    main(["caddy-sync"])
    config_file.write_text(json.dumps({"mcpServers": {}}))
    assert main(["caddy-sync"]) == 0
    assert host.block("existing-svc").exists()
    assert "no site block was removed" in capsys.readouterr().err


def test_caddy_sync_keeps_direct_sites(config_file, host):
    assert main(["add", "web", "--direct", "-p", "3000"]) == 0
    config_file.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    assert main(["caddy-sync"]) == 0
    assert "reverse_proxy 127.0.0.1:3000" in host.block("web").read_text()


def test_caddy_sync_fails_and_changes_nothing_when_caddy_does_not_reload(config_file, host, capsys):
    before = host.snapshot()
    host.caddy.fail("reload", "dial unix /run/caddy-webspec/admin.sock: connect: connection refused")
    assert main(["caddy-sync"]) == 1
    assert host.snapshot() == before
    assert "put back" in capsys.readouterr().err


def test_caddy_sync_reports_what_it_rewrote(config_file, host, capsys):
    main(["caddy-sync"])
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert "Unchanged: existing-svc" in out and "No change" in out
    assert "All Caddy configs up to date" not in out
    host.block("existing-svc").write_text("stale")
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert "Updated: existing-svc" in out and "Caddy reloaded" in out


# ── parser ──


def test_add_port_shorthand(config_file, capsys):
    rc = main(["add", "my-app", "--port", "20128"])
    assert rc == 0
    data = json.loads(config_file.read_text())
    svc = data["mcpServers"]["my-app"]
    assert svc["type"] == "http"
    # By IP: "localhost" may resolve to ::1 first, where any local user could listen.
    assert svc["url"] == "http://127.0.0.1:20128"
    assert "guard" not in svc  # --port implies --no-guard


def test_parser_add_defaults():
    parser = build_parser()
    args = parser.parse_args(["add", "my-svc", "--type", "http", "--url", "http://x"])
    assert args.name == "my-svc"
    assert args.svc_type == "http"
    assert args.guard is None  # unset: cmd_add resolves it (new → guarded; --force → keep existing)
    assert parser.parse_args(["add", "my-svc", "--url", "http://x"]).svc_type is None  # inferred


def test_parser_no_guard():
    parser = build_parser()
    args = parser.parse_args(["add", "my-svc", "--type", "http", "--url", "http://x", "--no-guard"])
    assert args.guard is False


# ── direct ──


def test_add_direct_with_port(config_file, host, capsys):
    """--direct -p PORT creates Caddy-only proxy, no ~/.claude.json entry."""
    rc = main(["add", "my-app", "--direct", "-p", "3000"])
    assert rc == 0

    # Should NOT be added to ~/.claude.json
    data = json.loads(config_file.read_text())
    assert "my-app" not in data["mcpServers"]

    # Should have Caddy config pointing directly at port 3000
    caddy_content = host.block("my-app").read_text()
    assert "reverse_proxy 127.0.0.1:3000" in caddy_content
    assert "rate_limit" not in caddy_content

    out = capsys.readouterr().out
    assert "direct" in out.lower()
    assert "3000" in out


def test_add_direct_with_url(config_file, host, probes):
    """--direct --url extracts port from URL."""
    probes.health = None
    rc = main(["add", "web-ui", "--direct", "--url", "http://localhost:8080"])
    assert rc == 0

    caddy_content = host.block("web-ui").read_text()
    assert "reverse_proxy 127.0.0.1:8080" in caddy_content  # localhost means the IPv4 loopback

    data = json.loads(config_file.read_text())
    assert "web-ui" not in data["mcpServers"]


def test_add_direct_requires_port_or_url(config_file, capsys):
    """--direct without --port or --url fails."""
    rc = main(["add", "bad-svc", "--direct", "--type", "http"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--direct requires" in err


@pytest.mark.parametrize("url,upstream", [
    ("http://[::1]:8080", "[::1]:8080"),
    ("http://127.0.0.1:8080/app/", "127.0.0.1:8080"),
    ("http://127.0.0.2:8080", "127.0.0.2:8080"),
])
def test_add_direct_honors_an_explicit_loopback_host(config_file, host, url, upstream):
    assert main(["add", "web-ui", "--direct", "--url", url]) == 0
    assert f"reverse_proxy {upstream}\n" in host.block("web-ui").read_text()


@pytest.mark.parametrize("argv,message", [
    (["--url", "http://192.168.1.5:8080"], "not a loopback address"),
    (["--url", "http://example.com:8080"], "not an IP address"),
    (["--url", "https://127.0.0.1:8443"], "must be http://"),
    (["--url", "localhost:8080"], "must be http://"),
    (["--url", "http://127.0.0.1"], "must include the port"),
    (["--url", "http://127.0.0.1:99999"], "invalid port"),
    (["--url", "http://127.0.0.1:8080", "--port", "9090"], "disagree"),
])
def test_add_direct_refuses_targets_off_loopback(config_file, host, argv, message, capsys):
    assert main(["add", "web-ui", "--direct", *argv]) == 1
    assert message in capsys.readouterr().err
    assert not host.block("web-ui").exists()
    assert host.caddy.calls() == []


# P12: Caddy's listeners and the gateway's port are not apps. Through :7003 each request would go
# round through Caddy until a header limit stopped it (431, ~500 connections each); the gateway's
# services have site blocks of their own. Caddy holds both loopback addresses, so any is refused.
@pytest.mark.parametrize("argv,owner", [
    (["--port", "7003"], "127.0.0.1:7003 is Caddy's direct listener (:7003)"),
    (["--port", "7001"], "127.0.0.1:7001 is Caddy's gateway listener (:7001)"),
    (["--port", "7002"], "127.0.0.1:7002 is the gateway's port (:7002)"),
    (["--url", "http://[::1]:7003"], "[::1]:7003 is Caddy's direct listener (:7003)"),
    (["--url", "http://localhost:7001"], "127.0.0.1:7001 is Caddy's gateway listener (:7001)"),
    (["--url", "http://127.0.0.2:7003/app"], "127.0.0.2:7003 is Caddy's direct listener (:7003)"),
])
def test_add_direct_refuses_caddy_and_the_gateway_as_the_app(config_file, host, argv, owner, capsys):
    assert main(["add", "loop", "--direct", *argv]) == 1
    err = capsys.readouterr().err
    assert f"{owner}, not an app: a direct site must point at the app itself, at the port it listens on" in err
    assert not host.block("loop").exists() and host.caddy.calls() == []


def test_add_direct_refuses_the_port_the_gateway_listens_on(config_file, host, monkeypatch, capsys):
    monkeypatch.setattr(ctl, "GATEWAY_PORT", 7102)  # WEBSPEC_INTERNAL_PORT
    assert main(["add", "loop", "--direct", "--port", "7102"]) == 1
    err = capsys.readouterr().err
    assert "the gateway's port (:7102)" in err and "to serve a gateway service, add it without --direct" in err
    assert not host.block("loop").exists()


def test_add_without_a_public_domain_serves_the_loopback_name_only(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch, domain="")
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(config))
    assert main(["add", "svc", "--type", "http", "--url", "http://x", "--guard"]) == 0
    block = caddy.CADDY_CONF_DIR.joinpath("svc.caddy").read_text()
    assert "http://svc.localhost:7001 {" in block
    assert "http://svc.localhost:7001 (no public domain" in capsys.readouterr().out


def test_domain_never_defaults_to_the_projects_own(monkeypatch, tmp_path):
    # sudo drops WEBSPEC_DOMAIN: the fallback is what the host records, else none.
    from webspec import config_writer
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    monkeypatch.setattr(caddy, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "missing.env")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "missing.json")
    assert caddy.public_domain()[0] == ""
    (tmp_path / "gateway.env").write_text("WEBSPEC_GUARD_KEY=k\nWEBSPEC_DOMAIN=example.org\n")
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "gateway.env")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "config.json")
    assert caddy.public_domain()[0] == "example.org"


def test_add_direct_refuses_the_name_of_a_gateway_service(config_file, host, capsys):
    # A direct site bypasses the gateway: it must not take a guarded service's host names.
    assert main(["add", "existing-svc", "--direct", "-p", "3000", "--force"]) == 1
    assert "is a gateway service" in capsys.readouterr().err
    assert not host.block("existing-svc").exists()


def test_add_direct_and_rm_work_without_any_config(host, probes, tmp_path, monkeypatch, capsys):
    # Through sudo on a host whose root has no ~/.claude.json: the command setup-caddy.sh suggests.
    monkeypatch.setenv("WEBSPEC_CONFIG", str(tmp_path / "missing.json"))
    assert main(["add", "llms", "--direct", "--port", "20128"]) == 0
    assert "reverse_proxy 127.0.0.1:20128" in host.block("llms").read_text()
    assert main(["rm", "llms"]) == 0
    assert not host.block("llms").exists()
    assert "no service entry removed" in capsys.readouterr().out


def test_parser_direct_flag():
    parser = build_parser()
    args = parser.parse_args(["add", "my-app", "--direct", "-p", "3000"])
    assert args.direct is True
    assert args.port == 3000


# ── https ──


def test_add_https_shows_https_url(config_file, capsys):
    rc = main(["add", "secure-svc", "--type", "http", "--url", "http://x", "--https"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://secure-svc.i-a-m.live" in out


def test_add_direct_https_shows_https_url(config_file, capsys):
    rc = main(["add", "my-app", "--direct", "-p", "3000", "--https"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://my-app.i-a-m.live" in out


def test_add_without_https_shows_http_url(config_file, capsys):
    rc = main(["add", "plain-svc", "--type", "http", "--url", "http://x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://plain-svc.i-a-m.live" in out


def test_add_force_keeps_security_settings(config_file, capsys):
    """--force must not silently drop level / tool overrides / labels."""
    cfg = json.loads(config_file.read_text())
    cfg["mcpServers"]["existing-svc"].update({"level": 4, "tools": {"x": {"tier": "dangerous"}}, "labels": ["eu"]})
    config_file.write_text(json.dumps(cfg))
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new", "--force"])
    assert rc == 0
    entry = _entries(config_file)["existing-svc"]
    assert entry["level"] == 4 and entry["tools"] == {"x": {"tier": "dangerous"}} and entry["labels"] == ["eu"]
    rc = main(["add", "existing-svc", "--type", "http", "--url", "http://new", "--force", "--level", "2"])
    assert _entries(config_file)["existing-svc"]["level"] == 2


def test_add_force_keeps_guard_unless_given(config_file):
    """Cloud review: --force without --no-guard silently escalated a level-0 service."""
    assert main(["add", "local", "--type", "http", "--url", "http://a", "--no-guard", "--level", "0"]) == 0
    assert main(["add", "local", "--type", "http", "--url", "http://b", "--force"]) == 0
    entry = _entries(config_file)["local"]
    assert "guard" not in entry and entry["level"] == 0
    assert main(["add", "fresh", "--type", "http", "--url", "http://c"]) == 0
    assert _entries(config_file)["fresh"]["guard"] is True


def test_ls_columns_align(config_file, probes, capsys):
    probes.health = None
    main(["ls"])
    config_line, header, _, row = capsys.readouterr().out.splitlines()[:4]
    assert config_line.startswith("Config:")
    assert header.index("HEALTH") == row.index("down")


def test_output_keeps_its_order_in_a_pipe(tmp_path):
    """In a pipe or a log, stdout is buffered and stderr is not: an end-to-end run saw an error
    before the Config: line that names the file it is about."""
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"svc": {"type": "http", "url": "http://127.0.0.1:9000/mcp"}}}))
    env = {**os.environ, "WEBSPEC_CONFIG": str(config), "WEBSPEC_ENV_FILE": str(tmp_path / ".env"),
           "HOME": str(tmp_path)}
    run = subprocess.run([sys.executable, "-m", "webspec.ctl", "add", "svc", "--port", "3000"],
                         cwd=Path(__file__).resolve().parents[1], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    assert run.returncode == 1
    assert run.stdout.splitlines()[:2] == [f"Config:  {config}",
                                          "Error: service 'svc' already exists (use --force to update)"], run.stdout
