"""webspec-ctl on real hosts: run as root through sudo, on migrated and unmigrated hosts, on macOS.

The rules (docs/spec/audit-deployment.md) these protect:
- DP-1, DP-4: as root, nothing runs from the caller's PATH, nor a caddy another user can replace.
- DP-5: the public domain the proxy serves is not dropped by a routine command run through sudo,
  and a change Caddy does not load is undone, never left to break its next start.
- DP-8: the env file only gets MCP-server secrets, never a WEBSPEC_* setting.

And what the commands report: a guarded service is served, not down (P13); a Caddyfile for other
sockets than systemd passes, and [::1] listeners that nothing holds, are named by health (P2); a
secret line that systemd ignores draws a warning that says which line to fix (P11).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading

import pytest

from webspec import caddy, config_writer, ctl
from webspec.caddy import SAFE_PATH, Site, read_site
from webspec.ctl import main
from tests.test_caddy_transaction import make_caddy_host


@pytest.fixture
def probes(monkeypatch):
    answers = {"health": ctl.OK, "ready": True}
    monkeypatch.setattr(ctl, "_health_check", lambda *a, **kw: answers["health"])
    monkeypatch.setattr(ctl, "_wait_for_gateway", lambda *a, **kw: answers["ready"])
    return answers


def _config(tmp_path, monkeypatch, servers: dict | None = None):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"mcpServers": servers or {}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(path))
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / "gateway.env"))
    return path


def _servers(path):
    return json.loads(path.read_text())["mcpServers"]


@pytest.fixture
def public_host(tmp_path, monkeypatch, probes):
    """Migrated with WEBSPEC_DOMAIN=i-a-m.live; a guarded and an unguarded service; no WEBSPEC_DOMAIN now."""
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    config = _config(tmp_path, monkeypatch, {"op-auth": {"command": "x", "guard": True},
                                             "mail-proton": {"command": "x"}})
    monkeypatch.setenv("WEBSPEC_DOMAIN", "i-a-m.live")
    assert main(["caddy-sync"]) == 0
    monkeypatch.delenv("WEBSPEC_DOMAIN")  # sudo drops it
    return host, config


def _first_line(path):
    return next(line for line in path.read_text().splitlines() if line.startswith("http://"))


# ── root: PATH ──


def test_main_as_root_runs_nothing_from_the_callers_path(tmp_path, monkeypatch, probes, capsys):
    monkeypatch.setenv("PATH", f"{tmp_path}/agent-bin{os.pathsep}/usr/bin")
    monkeypatch.setattr(ctl.os, "geteuid", lambda: 0)
    _config(tmp_path, monkeypatch)
    main(["ls"])
    assert os.environ["PATH"] == SAFE_PATH


def test_main_as_a_user_keeps_the_path(tmp_path, monkeypatch, probes):
    monkeypatch.setenv("PATH", f"{tmp_path}/bin{os.pathsep}/usr/bin")
    monkeypatch.setattr(ctl.os, "geteuid", lambda: 1000)
    _config(tmp_path, monkeypatch)
    main(["ls"])
    assert os.environ["PATH"] == f"{tmp_path}/bin{os.pathsep}/usr/bin"


# ── the public domain survives sudo ──


def test_a_routine_sync_through_sudo_keeps_the_public_addresses(public_host, capsys):
    host, _ = public_host
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert "i-a-m.live (recorded in" in out
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001, http://op-auth.i-a-m.live:7001 {"
    assert "No change" in out


def test_add_through_sudo_serves_the_recorded_domain(public_host, capsys):
    host, _ = public_host
    assert main(["add", "notes", "--command", "python3", "--level", "1"]) == 0
    assert "http://notes.i-a-m.live:7001" in _first_line(host.block("notes"))
    assert main(["add", "app", "--direct", "--port", "3000"]) == 0
    assert "http://app.i-a-m.live:7003" in _first_line(host.block("app"))  # the direct listener (DP-3)
    assert "URL:     http://app.i-a-m.live" in capsys.readouterr().out


@pytest.mark.parametrize("how", [["--no-public"], "WEBSPEC_DOMAIN="])
def test_dropping_the_public_addresses_takes_an_explicit_opt_out(public_host, monkeypatch, how, capsys):
    host, _ = public_host
    argv = ["caddy-sync"]
    if isinstance(how, list):
        argv += how
    else:
        monkeypatch.setenv("WEBSPEC_DOMAIN", "")
    capsys.readouterr()
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "Updated: op-auth" in out and "op-auth no longer serves op-auth.i-a-m.live" in out
    assert "(was i-a-m.live)" in out
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001 {"
    # The choice is recorded: the next routine sync keeps it.
    assert main(["caddy-sync"]) == 0
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001 {"


@pytest.fixture
def production_host(tmp_path, monkeypatch, probes):
    """A production host migrated with WEBSPEC_DOMAIN=i-a-m.live, whose gateway.env sets the same domain
    (the gateway reads it there), and a guarded service; webspec-ctl runs through sudo, without WEBSPEC_*."""
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    etc = tmp_path / "etc-webspec"
    etc.mkdir()
    config = etc / "config.json"
    config.write_text(json.dumps({"mcpServers": {"op-auth": {"command": "x", "guard": True}}}))
    (etc / "gateway.env").write_text("WEBSPEC_DOMAIN=i-a-m.live\n")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", config)
    monkeypatch.setattr(config_writer, "PRODUCTION_ENV", etc / "gateway.env")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", etc / "gateway.env")
    for name in ("WEBSPEC_CONFIG", "WEBSPEC_ENV_FILE", "WEBSPEC_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    assert main(["caddy-sync"]) == 0
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001, http://op-auth.i-a-m.live:7001 {"
    return host, config


def test_on_a_production_host_the_opt_out_outlasts_the_gateways_own_domain(production_host, monkeypatch, capsys):
    # gateway.env keeps WEBSPEC_DOMAIN, which the gateway needs: that must not undo --no-public.
    host, _ = production_host
    assert main(["caddy-sync", "--no-public"]) == 0
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001 {"
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001 {"
    assert "No change" in out and "restart" not in out  # no F31 hint: the gateway's domain did not change
    assert "sets WEBSPEC_DOMAIN=i-a-m.live" in out and "caddy-sync with WEBSPEC_DOMAIN=i-a-m.live" in out
    assert main(["add", "notes", "--command", "x", "--level", "1"]) == 0
    assert _first_line(host.block("notes")) == "http://notes.localhost:7001 {"
    capsys.readouterr()
    assert caddy.main(["domain"]) == 0  # setup-caddy.sh's own resolution keeps it too
    assert capsys.readouterr().out.startswith("\trecorded in ")
    # Serving the domain again takes WEBSPEC_DOMAIN. gateway.env already sets it: the hint says so.
    monkeypatch.setenv("WEBSPEC_DOMAIN", "i-a-m.live")
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert _first_line(host.block("op-auth")) == "http://op-auth.localhost:7001, http://op-auth.i-a-m.live:7001 {"
    assert "already sets WEBSPEC_DOMAIN=i-a-m.live" in out and "Set WEBSPEC_DOMAIN" not in out


def test_an_explicit_new_domain_moves_every_block_and_is_remembered(public_host, monkeypatch):
    host, _ = public_host
    monkeypatch.setenv("WEBSPEC_DOMAIN", "example.org")
    assert main(["caddy-sync"]) == 0
    monkeypatch.delenv("WEBSPEC_DOMAIN")
    assert main(["caddy-sync"]) == 0
    assert "http://op-auth.example.org:7001" in _first_line(host.block("op-auth"))
    assert caddy.recorded_domain()[0] == "example.org"


def test_add_never_moves_one_block_to_another_domain(public_host, monkeypatch, capsys):
    host, config = public_host
    monkeypatch.setenv("WEBSPEC_DOMAIN", "example.org")
    assert main(["add", "notes", "--command", "python3"]) == 1
    # The command that changes the domain of every block, spelled out: a bare caddy-sync would not.
    assert "`sudo WEBSPEC_DOMAIN=example.org /opt/webspec/venv/bin/webspec-ctl caddy-sync`" in capsys.readouterr().err
    assert "notes" not in _servers(config) and not host.block("notes").exists()
    monkeypatch.setenv("WEBSPEC_DOMAIN", "")
    assert main(["add", "notes", "--command", "python3"]) == 1
    assert "`sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync --no-public`" in capsys.readouterr().err


def test_a_host_migrated_before_the_record_is_sent_back_to_setup_caddy_which_keeps_its_domain(
        tmp_path, monkeypatch, probes, capsys):
    # setup-caddy.sh at fc0c558 put the domain in the blocks only, and gave direct sites no
    # listener of their own: webspec-ctl changes nothing there, and a re-run of setup-caddy.sh
    # (whose `py domain` is resolve_domain) keeps the domain the blocks serve.
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    _config(tmp_path, monkeypatch, {"op-auth": {"command": "x", "guard": True}})
    monkeypatch.setenv("WEBSPEC_DOMAIN", "i-a-m.live")
    assert main(["caddy-sync"]) == 0
    monkeypatch.delenv("WEBSPEC_DOMAIN")
    lines = host.caddyfile.read_text().split("\n")
    del lines[1]
    host.caddyfile.write_text("\n".join(lines))
    before = host.snapshot()
    capsys.readouterr()
    assert main(["caddy-sync"]) == 1
    assert "before direct sites had a listener of their own" in capsys.readouterr().err
    assert host.snapshot() == before
    assert caddy.resolve_domain().name == "i-a-m.live"


# ── refusals change nothing ──


_PRE_HARDENING_CADDYFILE = "{\n\tadmin localhost:2019\n}\nimport /etc/caddy/conf.d/*.caddy\n"


def test_every_command_that_writes_a_block_refuses_the_pre_hardening_layout(tmp_path, monkeypatch, probes, capsys):
    host = make_caddy_host(tmp_path, monkeypatch)
    host.caddyfile.write_text(_PRE_HARDENING_CADDYFILE)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "x"}})
    host.block("svc").write_text("http://svc.localhost:7001 {\n\treverse_proxy localhost:7002\n}\n")
    before = (config.read_text(), host.snapshot())
    for argv in (["add", "new", "--command", "x"], ["add", "web", "--direct", "--port", "3000"],
                 ["caddy-sync"]):
        assert main(argv) == 1, argv
        assert "pre-hardening Caddy layout" in capsys.readouterr().err
    assert (config.read_text(), host.snapshot()) == before
    assert host.caddy.calls() == []


def test_rm_revokes_a_service_even_where_caddy_cannot_be_changed(tmp_path, monkeypatch, probes, capsys):
    # The gateway entry is what revokes a service: the agent reaches the gateway itself
    # (127.0.0.1:7002) whatever Caddy does. The pre-hardening Caddy keeps its block, which only
    # forwards to a gateway that no longer serves the name.
    host = make_caddy_host(tmp_path, monkeypatch)
    host.caddyfile.write_text(_PRE_HARDENING_CADDYFILE)
    config = _config(tmp_path, monkeypatch, {"op-auth": {"command": "x", "level": 3}, "other": {"command": "x"}})
    env_file = tmp_path / "gateway.env"
    env_file.write_text("OP_TOKEN=abc\nOTHER_TOKEN=def\n")
    host.block("op-auth").write_text("http://op-auth.localhost:7001 {\n\treverse_proxy localhost:7002\n}\n")
    caddy_before = host.snapshot()
    assert main(["rm", "op-auth", "--clean-env", "OP_TOKEN"]) == 1  # scripts notice: Caddy was not changed
    out, err = capsys.readouterr()
    assert set(_servers(config)) == {"other"} and env_file.read_text() == "OTHER_TOKEN=def\n"
    assert "Removed 'op-auth'" in out and "Removed OP_TOKEN" in out and "deprovisioned" in out
    assert "Caddy's site block for op-auth was left in place" in err and "pre-hardening Caddy layout" in err
    assert "no longer serves op-auth" in err and "404" in err and "caddy-sync" in err
    assert "A site block written now" not in err  # the refusal fits a removal
    assert host.snapshot() == caddy_before and host.caddy.calls() == []


def test_rm_of_a_name_caddy_alone_serves_changes_nothing_where_caddy_cannot_be_changed(
        tmp_path, monkeypatch, probes, capsys):
    # A direct site lives in Caddy only: there is nothing to revoke in the gateway, so nothing goes.
    host = make_caddy_host(tmp_path, monkeypatch)
    host.caddyfile.write_text(_PRE_HARDENING_CADDYFILE)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "x"}})
    host.block("web").write_text("http://web.localhost:7001 {\n\treverse_proxy localhost:3000\n}\n")
    before = (config.read_text(), host.snapshot())
    assert main(["rm", "web"]) == 1
    err = capsys.readouterr().err
    assert "Caddy's site block for web was not removed" in err and "Nothing was changed" in err
    assert (config.read_text(), host.snapshot()) == before


def test_rm_needs_no_caddy_change_for_a_name_without_a_block(tmp_path, monkeypatch, probes, capsys):
    host = make_caddy_host(tmp_path, monkeypatch)
    host.caddyfile.write_text(_PRE_HARDENING_CADDYFILE)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "x"}})
    assert main(["rm", "svc"]) == 0
    assert "No Caddy site block for svc" in capsys.readouterr().out
    assert _servers(config) == {} and host.caddy.calls() == []


def test_add_takes_the_entry_back_out_when_caddy_does_not_load_the_block(tmp_path, monkeypatch, probes, capsys):
    host = make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "old"}})
    before = config.read_text()
    host.caddy.fail("reload", "loading new config: listening on fd/4: socket operation on non-socket")
    assert main(["add", "new", "--command", "x"]) == 1
    assert main(["add", "svc", "--command", "new", "--force"]) == 1
    assert json.loads(config.read_text()) == json.loads(before)
    assert not host.block("new").exists() and not host.block("svc").exists()
    assert "nothing was changed" in capsys.readouterr().err


def test_rm_revokes_the_service_when_caddy_does_not_drop_the_block(public_host, capsys):
    # Caddy keeps the block it runs with (every file is put back), and the gateway entry goes.
    host, config = public_host
    caddy_before = host.snapshot()
    host.caddy.fail("reload", "boom")
    assert main(["rm", "op-auth"]) == 1
    out, err = capsys.readouterr()
    assert "op-auth" not in _servers(config) and "Removed 'op-auth'" in out
    assert host.snapshot() == caddy_before
    assert "Caddy's site block for op-auth was left in place" in err and "put back" in err and "boom" in err


# ── macOS: no Caddy to manage ──


def test_macos_edits_the_config_and_leaves_caddy_out(tmp_path, monkeypatch, probes, capsys):
    host = make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.setattr(caddy, "_on_macos", lambda: True)
    config = _config(tmp_path, monkeypatch, {"old": {"command": "x"}})
    assert main(["add", "svc", "--command", "x"]) == 0
    assert main(["rm", "old"]) == 0
    out = capsys.readouterr().out
    assert out.count("Caddy:   skipped: macOS") == 2
    assert set(_servers(config)) == {"svc"}
    assert main(["add", "web", "--direct", "--port", "3000"]) == 1
    assert main(["caddy-sync"]) == 1
    assert capsys.readouterr().err.count("macOS") == 2
    assert list(host.conf.iterdir()) == [] and host.caddy.calls() == []


def test_a_host_without_caddy_edits_the_config_only(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch, domain=None)
    config = _config(tmp_path, monkeypatch)
    assert main(["add", "svc", "--command", "x"]) == 0
    assert "Caddy:   skipped: there is no" in capsys.readouterr().out
    assert "svc" in _servers(config)


# ── the gateway config: chosen once, printed by every command (C3) ──


@pytest.mark.parametrize("argv", [["ls"], ["health"], ["rm", "svc"], ["caddy-sync"],
                                  ["add", "svc2", "--command", "x"]])
def test_every_command_prints_the_config_it_uses(tmp_path, monkeypatch, probes, argv, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "x"}})
    main(argv)
    assert f"Config:  {config}" in capsys.readouterr().out


def test_a_production_host_is_one_with_etc_webspec_config_json(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.delenv("WEBSPEC_CONFIG")
    monkeypatch.delenv("WEBSPEC_ENV_FILE")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".claude.json").write_text(json.dumps({"mcpServers": {"dev": {"command": "x"}}}))
    etc = tmp_path / "etc-webspec"
    etc.mkdir()
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", etc / "config.json")
    monkeypatch.setattr(config_writer, "PRODUCTION_ENV", etc / "gateway.env")
    (etc / "gateway.env").write_text("WEBSPEC_DOMAIN=example.com\n")
    assert main(["ls"]) == 0  # /etc/webspec without config.json: still the dev gateway's config
    assert "dev" in capsys.readouterr().out
    (etc / "config.json").write_text(json.dumps({"mcpServers": {"prod": {"command": "x"}}}))
    assert main(["ls"]) == 0
    out = capsys.readouterr().out
    assert f"Config:  {etc / 'config.json'}" in out and "prod" in out and "dev " not in out


def test_health_reads_the_config_the_other_commands_use(tmp_path, monkeypatch, probes, capsys):
    # Through sudo on a production host: not root's ~/.claude.json, which does not exist.
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.delenv("WEBSPEC_CONFIG")
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    production = tmp_path / "config.json"
    production.write_text(json.dumps({"mcpServers": {"notes": {"command": "x", "level": 2}}}))
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", production)
    assert main(["health", "notes"]) == 0
    out = capsys.readouterr().out
    assert "Registry:     found" in out and "Level:        2 bound" in out and "Guard:        yes" in out


def test_health_reports_a_caddyfile_for_other_sockets_than_systemd_passes(tmp_path, monkeypatch, probes, capsys):
    # P2: booted with ipv6.disable=1 after setup, systemd passes the IPv4 sockets only, and the
    # Caddyfile written for both families cannot start. Only the journal said so.
    make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")  # generated for "dual"
    _config(tmp_path, monkeypatch, {"notes": {"command": "x", "level": 1}})
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: caddy.IPV4_ONLY.addresses)
    assert main(["health"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith(f"Caddy:   {caddy.CADDYFILE} binds the sockets of the 'dual' layout, but "
                               "caddy-webspec.socket passes 127.0.0.1:7001, 127.0.0.1:7003 ('ipv4'): Caddy cannot "
                               "start")
    assert "Run gateway/tools/setup-caddy.sh again" in lines[1] and "--- notes ---" in lines
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: caddy.DUAL.addresses)
    assert main(["health"]) == 0
    assert not any(line.startswith("Caddy:") for line in capsys.readouterr().out.splitlines())


def test_health_reports_ipv6_listeners_that_nothing_holds(tmp_path, monkeypatch, probes, capsys):
    # P2, the other way round: set up by an earlier setup-caddy.sh on a kernel without IPv6, whose
    # drop-in kept the unit's IPv4 lines alone, then booted with IPv6. The Caddyfile fits what
    # systemd passes, so nothing else says that [::1]:7001 and [::1]:7003 are anyone's to take.
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    host.caddyfile.write_text(caddy.generate_global_caddyfile(conf_dir=host.conf, domain="i-a-m.live",
                                                              listen=caddy.IPV4_ONLY))
    dropin = tmp_path / "caddy-webspec.socket.d" / "10-ipv4-only.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Socket]\nListenStream=\n")
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", dropin)
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: caddy.IPV4_ONLY.addresses)
    _config(tmp_path, monkeypatch, {"notes": {"command": "x", "level": 1}})
    assert main(["health"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == f"Caddy:   {caddy.unheld_listeners()}"
    assert "Nothing holds [::1]:7001 and [::1]:7003" in lines[1] and str(dropin) in lines[1]
    assert sum(line.startswith("Caddy:") for line in lines) == 1 and "--- notes ---" in lines
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: False)  # the kernel it was set up for
    assert main(["health"]) == 0
    assert not any(line.startswith("Caddy:") for line in capsys.readouterr().out.splitlines())


@pytest.mark.parametrize("argv", [["ls"], ["health"], ["add", "svc", "--command", "x"]])
def test_a_missing_config_is_an_error_not_a_traceback(tmp_path, monkeypatch, probes, argv, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.setenv("WEBSPEC_CONFIG", str(tmp_path / "missing.json"))
    assert main(argv) == 1
    assert "does not exist" in capsys.readouterr().err


def test_rm_only_claims_what_it_removed(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch, {"Notes_Svc": {"command": "x"}})
    (tmp_path / "gateway.env").write_text("export NOTES_TOKEN='s3cr3t'\n")
    assert main(["rm", "other", "--clean-env", "OTHER_TOKEN"]) == 0
    out = capsys.readouterr().out
    assert "'other' is not in" in out and "OTHER_TOKEN is not set in" in out
    assert "Removed" not in out and "deprovisioned" not in out
    assert main(["rm", "notes-svc", "--clean-env", "NOTES_TOKEN"]) == 0  # matched by normalized name
    out = capsys.readouterr().out
    assert "Removed 'Notes_Svc'" in out and "Removed NOTES_TOKEN" in out and "deprovisioned" in out
    assert _servers(config) == {} and (tmp_path / "gateway.env").read_text() == "\n"


# ── secrets ──


@pytest.mark.parametrize("name", ["WEBSPEC_HOST", "WEBSPEC_AUDIT_LOG", "MY-TOKEN"])
def test_add_refuses_a_secret_name_that_is_not_an_mcp_secret(tmp_path, monkeypatch, probes, name, capsys):
    host = make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch)
    assert main(["add", "svc", "--command", "x", "--secret", name]) == 1
    assert "--secret" in capsys.readouterr().err
    assert _servers(config) == {} and not (tmp_path / "gateway.env").exists()
    assert host.caddy.calls() == []


def test_add_says_what_it_did_with_a_secret(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    _config(tmp_path, monkeypatch)
    env = tmp_path / "gateway.env"
    env.write_text("OTHER=1\nTOKEN=already\n")
    assert main(["add", "svc", "--command", "x", "--secret", "TOKEN", "--secret", "NEW_TOKEN"]) == 0
    out, err = capsys.readouterr()
    assert f"TOKEN is already set in {env}" in out and f"Added placeholder TOKEN to" not in out
    assert f"Added placeholder NEW_TOKEN to {env}" in out and "Warning" not in err
    assert env.read_text() == "OTHER=1\nTOKEN=already\nNEW_TOKEN=\n"


def _linux_host(tmp_path, monkeypatch, *, production: bool) -> tuple:
    """A Linux host through sudo, without WEBSPEC_*: the production install, or a development host (HOME)."""
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.setattr(config_writer.sys, "platform", "linux")
    for name in ("WEBSPEC_CONFIG", "WEBSPEC_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    etc = tmp_path / "etc-webspec"
    etc.mkdir()
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", etc / "config.json")
    monkeypatch.setattr(config_writer, "PRODUCTION_ENV", etc / "gateway.env")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", etc / "gateway.env")
    config = etc / "config.json" if production else home / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {}}))
    return config, etc / "gateway.env" if production else home / ".env"


def test_on_linux_add_warns_of_a_secret_that_systemd_ignores(tmp_path, monkeypatch, probes, capsys):
    # P11: `export TOKEN=…` in the production gateway.env, a systemd EnvironmentFile=, sets
    # nothing. The warning names the line to change, never its value, and no placeholder goes in
    # after it: changed in place, as the warning says, the line would come before an empty
    # TOKEN=, and systemd keeps the last assignment.
    _, env = _linux_host(tmp_path, monkeypatch, production=True)
    env.write_text("WEBSPEC_GUARD_KEY=k\nexport TOKEN='s3cr3t'\n")
    assert main(["add", "svc", "--command", "x", "--secret", "TOKEN"]) == 0
    out, err = capsys.readouterr()
    assert "Added placeholder" not in out and "already set" not in out
    assert (f"Warning: {env} sets TOKEN only with `export TOKEN=…` (line 2), which systemd ignores: it reads "
            "KEY=value lines only") in err
    assert err.rstrip().endswith("The gateway does not get TOKEN from this file. Change line 2 to TOKEN=value.")
    assert "s3cr3t" not in out + err
    assert env.read_text() == "WEBSPEC_GUARD_KEY=k\nexport TOKEN='s3cr3t'\n"
    # The operator changes line 2 in place: the one line systemd reads holds the secret.
    env.write_text("WEBSPEC_GUARD_KEY=k\nTOKEN='s3cr3t'\n")
    assert caddy.read_env_file_value(env, "TOKEN") == "s3cr3t"
    assert main(["add", "svc2", "--command", "x", "--secret", "TOKEN"]) == 0
    out, err = capsys.readouterr()
    assert f"TOKEN is already set in {env}" in out and "Warning" not in err
    assert env.read_text() == "WEBSPEC_GUARD_KEY=k\nTOKEN='s3cr3t'\n"


def test_on_a_linux_development_host_add_keeps_the_shells_export_line(tmp_path, monkeypatch, probes, capsys):
    # P11's rule is systemd's, for the files systemd reads: a development host's ~/.env is a shell's
    # file, where `export TOKEN=…` sets TOKEN. An empty TOKEN= after it would blank the secret for
    # every shell that sources the file.
    _, env = _linux_host(tmp_path, monkeypatch, production=False)
    env.write_text("export TOKEN='s3cr3t'\n")
    assert main(["add", "svc", "--command", "x", "--secret", "TOKEN"]) == 0
    out, err = capsys.readouterr()
    assert f"TOKEN is already set in {env}" in out and "Warning" not in err
    assert env.read_text() == "export TOKEN='s3cr3t'\n"


def test_rm_refuses_to_clean_a_gateway_setting(tmp_path, monkeypatch, probes, capsys):
    make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch, {"svc": {"command": "x"}})
    (tmp_path / "gateway.env").write_text("WEBSPEC_GUARD_KEY=k\n")
    assert main(["rm", "svc", "--clean-env", "WEBSPEC_GUARD_KEY"]) == 1
    assert "gateway setting" in capsys.readouterr().err
    assert "svc" in _servers(config) and (tmp_path / "gateway.env").read_text() == "WEBSPEC_GUARD_KEY=k\n"


# ── level and guard: one value ──


def test_level_0_is_unguarded_and_local(public_host, capsys):
    host, config = public_host
    assert main(["add", "lvl0", "--command", "x", "--level", "0"]) == 0
    assert "guard" not in _servers(config)["lvl0"] and _servers(config)["lvl0"]["level"] == 0
    assert _first_line(host.block("lvl0")) == "http://lvl0.localhost:7001 {"
    out = capsys.readouterr().out
    assert "Guard:   no" in out and "URL:     http://lvl0.localhost:7001\n" in out


def test_level_0_with_guard_is_refused(public_host, capsys):
    host, config = public_host
    assert main(["add", "lvl0", "--command", "x", "--level", "0", "--guard"]) == 1
    assert "--level 0" in capsys.readouterr().err
    assert "lvl0" not in _servers(config) and not host.block("lvl0").exists()


@pytest.mark.parametrize("argv", [["--level", "2", "--no-guard"], ["--no-guard"]])
def test_no_guard_at_a_guarded_level_is_refused(public_host, argv, capsys):
    host, config = public_host
    if argv == ["--no-guard"]:  # the level comes from the existing entry, which --force keeps
        config.write_text(json.dumps({"mcpServers": {"lvl2": {"command": "x", "level": 2}}}))
        argv = argv + ["--force"]
    assert main(["add", "lvl2", "--command", "x", *argv]) == 1
    assert "every level" in capsys.readouterr().err
    assert not host.block("lvl2").exists()


def test_a_guarded_level_gets_the_public_host_whatever_the_flags(public_host, capsys):
    host, config = public_host
    assert main(["add", "lvl2", "--port", "9000", "--level", "2"]) == 0
    assert _servers(config)["lvl2"]["guard"] is True
    assert read_site(host.block("lvl2")) == Site("lvl2", "service", guard=True)
    assert "http://lvl2.i-a-m.live:7001" in _first_line(host.block("lvl2"))
    out = capsys.readouterr().out
    assert "Guard:   yes" in out and "URL:     http://lvl2.i-a-m.live" in out
    # caddy-sync, which reads the gateway's view, agrees: nothing to change.
    assert main(["caddy-sync"]) == 0 and "No change" in capsys.readouterr().out


def test_ls_shows_level_0_services_on_their_loopback_name(public_host, probes, capsys):
    capsys.readouterr()
    assert main(["ls"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[3:]}
    assert rows["mail-proton"].endswith("mail-proton.localhost:7001")
    assert rows["op-auth"].endswith("op-auth.i-a-m.live")


# ── --type ──


def test_command_alone_means_a_stdio_server(tmp_path, monkeypatch, probes):
    make_caddy_host(tmp_path, monkeypatch)
    config = _config(tmp_path, monkeypatch)
    assert main(["add", "notes1", "--command", "/opt/webspec/venv/bin/python", "--args", "demo.py",
                 "--level", "1"]) == 0
    assert _servers(config)["notes1"]["type"] == "stdio"
    assert main(["add", "remote", "--command", "x", "--url", "http://127.0.0.1:9000/mcp"]) == 0
    assert _servers(config)["remote"]["type"] == "http"


# ── health probes: by IP, with the Host header ──


class _Recorder(http.server.BaseHTTPRequestHandler):
    hosts: list[str] = []
    status = 200

    def do_HEAD(self):
        type(self).hosts.append(self.headers.get("Host"))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def loopback_server():
    _Recorder.hosts = []
    _Recorder.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_probes_dial_the_ip_and_name_the_host(loopback_server, monkeypatch):
    # *.localhost need not resolve (stock Debian), and must not: ::1 could be another user's.
    real = socket.getaddrinfo

    def no_localhost_names(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".localhost"):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_localhost_names)
    port = loopback_server
    assert ctl._health_check("notes", port=port, timeout=2) == ctl.OK
    assert ctl._wait_for_gateway("notes", port=port, max_wait=5) is True
    assert _Recorder.hosts == [f"notes.localhost:{port}"] * 2


# A guarded service answers 401 to a request without its guard, HEAD / included (DS-2): it is
# served (P13). Caddy's catch-all (421), an unknown service (404), the rate limit (429) and a
# 5xx are not.
@pytest.mark.parametrize("status,found,ready", [(200, ctl.OK, True), (204, ctl.OK, True),
                                                (401, ctl.GUARDED, True), (403, ctl.GUARDED, True),
                                                (404, None, False), (421, None, False), (429, None, False),
                                                (502, None, False), (503, None, False)])
def test_probe_statuses(loopback_server, status, found, ready):
    _Recorder.status = status
    assert ctl._health_check("svc", port=loopback_server, timeout=2) == found
    assert ctl._wait_for_gateway("svc", port=loopback_server, max_wait=0.5) is ready


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_without_caddy_the_gateway_is_also_looked_for_on_its_own_default_port(
        tmp_path, monkeypatch, loopback_server, capsys):
    # The macOS development agent (gateway/launchd/) runs `python -m webspec` without
    # WEBSPEC_INTERNAL_PORT, so its gateway listens on 7001, not on 7002 as behind Caddy.
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.setattr(caddy, "_on_macos", lambda: True)
    _config(tmp_path, monkeypatch, {"notes": {"command": "x", "level": 1}})
    monkeypatch.setattr(ctl, "CADDY_PORT", loopback_server)  # where the development gateway answers
    monkeypatch.setattr(ctl, "GATEWAY_PORT", _free_port())  # nothing there
    assert main(["ls"]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("notes "))
    assert row.split()[4] == "ok"  # NAME TYPE LEVEL (two words) HEALTH URL
    assert main(["health", "notes"]) == 0
    out = capsys.readouterr().out
    assert "Gateway:      reachable" in out and "Caddy proxy:  none on this host" in out
    assert ctl._wait_for_gateway("notes", port=ctl.GATEWAY_PORT, also=(loopback_server,), max_wait=5) is True


def test_with_caddy_the_gateway_is_only_looked_for_behind_it(tmp_path, monkeypatch, loopback_server, capsys):
    # Caddy's port answers for Caddy there: a gateway that is down must not look reachable.
    make_caddy_host(tmp_path, monkeypatch)
    _config(tmp_path, monkeypatch, {"notes": {"command": "x", "level": 1}})
    monkeypatch.setattr(ctl, "CADDY_PORT", loopback_server)
    monkeypatch.setattr(ctl, "GATEWAY_PORT", _free_port())
    assert main(["health", "notes"]) == 0
    out = capsys.readouterr().out
    assert "Caddy proxy:  reachable" in out and "Gateway:      unreachable" in out


def test_nothing_listening_is_unhealthy():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert ctl._health_check("svc", port=port, timeout=1) is None


# ── P13: a guarded service is served, not down ──


@pytest.fixture
def guarded_host(tmp_path, monkeypatch, loopback_server):
    """A migrated host whose Caddy and gateway both answer every probe from loopback_server, and a level-2 service."""
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    _config(tmp_path, monkeypatch, {"notes": {"command": "x", "level": 2, "guard": True},
                                    "pub": {"command": "x", "level": 0}})
    monkeypatch.setattr(ctl, "CADDY_PORT", loopback_server)
    monkeypatch.setattr(ctl, "GATEWAY_PORT", loopback_server)
    return host


def _row(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(f"{name} "))


def test_guarded_services_are_reported_served_not_down(guarded_host, capsys):
    _Recorder.status = 401  # {"error": "guard_missing"}: the gateway serves the name and wants the guard
    assert main(["ls"]) == 0
    out = capsys.readouterr().out
    header = next(line for line in out.splitlines() if line.startswith("NAME"))
    assert " ok (guarded) " in _row(out, "notes") and header.index("HEALTH") == _row(out, "notes").index("ok (")
    assert header.index("URL") == _row(out, "notes").index("notes.i-a-m.live")  # the columns still align
    assert "ok (guarded): the gateway serves the name, and answers 401 to a request without its guard" in out
    assert main(["health", "notes"]) == 0
    out = capsys.readouterr().out
    assert "Caddy proxy:  reachable (guarded)" in out and "Gateway:      reachable (guarded)" in out
    assert "unreachable" not in out and "reachable (guarded): the gateway serves the name" in out
    assert main(["add", "notes2", "--command", "x", "--level", "1"]) == 0
    out = capsys.readouterr().out
    assert "ready" in out and "  Health:  ok (guarded)\n" in out


@pytest.mark.parametrize("status", [503, 502, 404, 421])
def test_a_service_that_does_not_answer_well_is_still_down(guarded_host, status, capsys):
    _Recorder.status = status
    assert main(["ls"]) == 0
    out = capsys.readouterr().out
    assert _row(out, "notes").split()[4] == "down" and "(guarded)" not in out
    assert main(["health", "notes"]) == 0
    out = capsys.readouterr().out
    assert "Caddy proxy:  unreachable" in out and "Gateway:      unreachable" in out


def test_an_unguarded_service_is_plainly_ok(guarded_host, capsys):
    _Recorder.status = 200
    assert main(["ls"]) == 0
    out = capsys.readouterr().out
    assert _row(out, "pub").split()[4] == "ok" and "(guarded)" not in out


def test_without_caddy_add_reports_a_guarded_gateway_too(tmp_path, monkeypatch, loopback_server, capsys):
    # The macOS gateway, behind no proxy of webspec-ctl's: the gateway's own answer decides.
    make_caddy_host(tmp_path, monkeypatch)
    monkeypatch.setattr(caddy, "_on_macos", lambda: True)
    _config(tmp_path, monkeypatch)
    monkeypatch.setattr(ctl, "GATEWAY_PORT", loopback_server)
    _Recorder.status = 401
    assert main(["add", "notes", "--command", "x", "--level", "2"]) == 0
    assert "  Health:  ok (guarded) (gateway; no Caddy here)\n" in capsys.readouterr().out
