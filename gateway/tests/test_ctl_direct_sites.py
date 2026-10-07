"""webspec-ctl and the two Caddy listeners (contract C2).

- F8 (DP-3): a direct site proxies around the gateway, so it is served on the direct listener
  (127.0.0.1:7003, [::1]:7003), never on the gateway's (:7001), the only one the agent's egress
  allow-list names. add --direct says so, and prints the cloudflared rule the site needs.
- F26: the sockets a block binds are the ones the installed caddy-webspec.socket passes, as
  systemd reports them; webspec-ctl never asks the kernel whether it has IPv6.
- F31: after a domain change, the gateway must be restarted to serve the new hosts.
- P10: a gateway service takes a direct site's name only with --force, and add and caddy-sync
  say where its hosts went and that cloudflared's rule to :7003 must go, as rm does.
- P12: a direct site never proxies to Caddy's listeners or the gateway's port.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from webspec import caddy, ctl
from webspec.caddy import DUAL, IPV4_ONLY, Site, generate_direct_site_block, read_site, write_site_block
from webspec.ctl import main
from tests.test_caddy_transaction import make_caddy_host


@pytest.fixture
def probes(monkeypatch):
    """The gateway and Caddy answer; records the ports Caddy is probed on."""
    seen = []

    def health(name, port=ctl.CADDY_PORT, timeout=5.0):
        seen.append((name, port))
        return ctl.OK

    monkeypatch.setattr(ctl, "_health_check", health)
    monkeypatch.setattr(ctl, "_wait_for_gateway", lambda *a, **kw: True)
    return seen


def _host(tmp_path, monkeypatch, domain="i-a-m.live", layout=DUAL):
    host = make_caddy_host(tmp_path, monkeypatch, domain=domain)
    if layout is not DUAL:
        monkeypatch.setattr(caddy, "_systemd_listen", lambda: layout.addresses)
        host.caddyfile.write_text(caddy.generate_global_caddyfile(conf_dir=host.conf, domain=domain, listen=layout))
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"op-auth": {"command": "x", "guard": True}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(config))
    monkeypatch.setenv("WEBSPEC_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(ctl, "CADDY_PORT", 7001)
    monkeypatch.setattr(ctl, "GATEWAY_PORT", 7002)
    # F26: whatever the kernel would say, it is never asked.
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: pytest.fail("webspec-ctl asked the kernel about IPv6"))
    return host


def _addresses(block_path):
    return next(line for line in block_path.read_text().splitlines() if line.startswith("http://"))


def _binds(block_path):
    return re.findall(r"^\tbind (.+)$", block_path.read_text(), re.M)


# ── F8: add --direct ──


def test_a_direct_site_is_served_on_the_direct_listener_and_says_what_that_means(tmp_path, monkeypatch, probes,
                                                                                capsys):
    host = _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    assert _addresses(host.block("web")) == "http://web.localhost:7003, http://web.i-a-m.live:7003 {"
    assert _binds(host.block("web")) == ["fd/4 fd/6"]
    assert probes == [("web", 7003)]  # its health is checked where it is served
    out = capsys.readouterr().out
    assert "  Listen:  127.0.0.1:7003 and [::1]:7003 (the direct listener)" in out
    assert "  Type:    direct (bypasses the gateway)" in out
    notice = out[out.index("DP-3:"):]
    assert "web bypasses the gateway (no guard, no audit log)" in notice
    assert "Keep that port out of the agent's egress allow-list" in notice
    assert "which names the gateway's listener only: 127.0.0.1:7001 and [::1]:7001." in notice
    # The cloudflared rule it needs, ready to paste above the domain's wildcard.
    assert notice.splitlines()[-2:] == ["  - hostname: web.i-a-m.live", "    service: http://127.0.0.1:7003"]
    assert "send web.i-a-m.live to the direct listener with this ingress rule, placed above\n" \
           "the rule for *.i-a-m.live, then restart cloudflared" in notice


def test_without_a_public_domain_a_direct_site_needs_no_tunnel_rule(tmp_path, monkeypatch, probes, capsys):
    host = _host(tmp_path, monkeypatch, domain="")
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    assert _addresses(host.block("web")) == "http://web.localhost:7003 {"
    out = capsys.readouterr().out
    assert "No public domain: web is served to local processes only, as http://web.localhost:7003." in out
    assert "hostname:" not in out
    assert "  URL:     http://web.localhost:7003 (no public domain is configured)" in out


def test_on_an_ipv4_only_host_the_direct_site_binds_fd_4_alone(tmp_path, monkeypatch, probes, capsys):
    host = _host(tmp_path, monkeypatch, layout=IPV4_ONLY)
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    assert _binds(host.block("web")) == ["fd/4"]
    out = capsys.readouterr().out
    assert "  Listen:  127.0.0.1:7003 (the direct listener)" in out
    assert "which names the gateway's listener only: 127.0.0.1:7001." in out


def test_a_service_is_served_on_the_gateways_listener(tmp_path, monkeypatch, probes):
    host = _host(tmp_path, monkeypatch)
    assert main(["add", "notes", "--command", "python3", "--level", "1"]) == 0
    assert _addresses(host.block("notes")) == "http://notes.localhost:7001, http://notes.i-a-m.live:7001 {"
    assert _binds(host.block("notes")) == ["fd/3 fd/5"]
    assert probes == [("notes", 7001)]


def test_removing_a_direct_site_reminds_of_its_tunnel_rule(tmp_path, monkeypatch, probes, capsys):
    host = _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    capsys.readouterr()
    assert main(["rm", "web"]) == 0
    assert not host.block("web").exists()
    assert "web was a direct site: remove its cloudflared ingress rule to :7003 (hostname: web.i-a-m.live) too, " \
           "then restart cloudflared" in capsys.readouterr().out


# ── P10: a gateway service that takes a direct site's name ──

_REMINDER = "web was a direct site: remove its cloudflared ingress rule to :7003 (hostname: web.i-a-m.live) too"


def test_add_refuses_to_replace_a_direct_site_unless_forced(tmp_path, monkeypatch, probes, capsys):
    # The reverse of add --direct refusing a gateway service's name: the app's proxy is not lost
    # by accident, and when it is replaced, cloudflared's rule to :7003 must go as with rm.
    host = _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "8080"]) == 0
    config = tmp_path / "claude.json"
    before = (config.read_text(), host.snapshot())
    capsys.readouterr()
    assert main(["add", "web", "--command", "x", "--level", "1"]) == 1
    err = capsys.readouterr().err
    assert "'web' is a direct site in Caddy (-> 127.0.0.1:8080); a gateway service of that name would " \
           "replace it. Choose another name, or pass --force to replace it." in err
    assert (config.read_text(), host.snapshot()) == before
    assert main(["add", "web", "--command", "x", "--level", "1", "--force"]) == 0
    out = capsys.readouterr().out
    assert read_site(host.block("web")) == Site("web", "service", guard=True)
    assert "replaced the direct site web (-> 127.0.0.1:8080)" in out and _REMINDER in out


def test_a_name_no_block_can_have_is_an_error_not_a_traceback(tmp_path, monkeypatch, probes, capsys):
    _host(tmp_path, monkeypatch)
    assert main(["add", "x" * 64, "--command", "x"]) == 1  # longer than a DNS label
    assert "is not a lowercase DNS label" in capsys.readouterr().err
    assert "x" * 64 not in json.loads((tmp_path / "claude.json").read_text())["mcpServers"]


def test_add_of_a_new_name_says_nothing_of_direct_sites(tmp_path, monkeypatch, probes, capsys):
    _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "8080"]) == 0
    capsys.readouterr()
    assert main(["add", "notes", "--command", "x", "--level", "1"]) == 0
    out = capsys.readouterr().out
    assert "direct site" not in out and "cloudflared" not in out


@pytest.mark.parametrize("level,served,dropped", [(1, "web.localhost, web.i-a-m.live", None),
                                                  (0, "web.localhost", "web.i-a-m.live")])
def test_sync_of_a_service_that_replaces_a_direct_site_sends_cloudflared_back(
        tmp_path, monkeypatch, probes, level, served, dropped, capsys):
    # The config edited by hand, then caddy-sync: the hosts leave the direct listener. The old
    # advice (add a rule to :7003, "moved to the direct listener") sent them to a 421.
    host = _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "8080"]) == 0
    config = tmp_path / "claude.json"
    servers = json.loads(config.read_text())["mcpServers"]
    servers["web"] = {"command": "x", "level": level}
    config.write_text(json.dumps({"mcpServers": servers}))
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert read_site(host.block("web")).kind == "service"
    assert "moved to the direct listener" not in out and "service: http://127.0.0.1:7003" not in out
    assert f"web is no longer a direct site: now served through the gateway, on 127.0.0.1:7001 and [::1]:7001: " \
           f"{served}\n" in out
    if dropped:
        assert f"web no longer serves {dropped}\n" in out
    assert out.rstrip().splitlines()[-1].startswith(_REMINDER)
    assert main(["caddy-sync"]) == 0 and "direct site" not in capsys.readouterr().out  # said once


def test_sync_plans_the_direction_of_each_move(tmp_path, monkeypatch, probes):
    host = _host(tmp_path, monkeypatch)
    old = generate_direct_site_block("old", "i-a-m.live", target_port=3000).replace(":7003", ":7001")
    write_site_block("old", old.replace("bind fd/4 fd/6", "bind fd/3 fd/5"), conf_dir=host.conf)  # fc0c558's
    assert main(["add", "web", "--direct", "--port", "8080"]) == 0
    services = {"op-auth": SimpleNamespace(guard=True), "web": SimpleNamespace(guard=True)}
    result = caddy.plan_sync(services, "i-a-m.live", listen=DUAL)
    assert result.moved == {"old": ["old.localhost", "old.i-a-m.live"]}  # to the direct listener
    assert result.left_direct == {"web": ["web.localhost", "web.i-a-m.live"]}  # from it


# ── P12: a direct site never proxies to Caddy or the gateway ──


def test_sync_points_out_a_direct_site_written_to_loop_through_caddy(tmp_path, monkeypatch, probes, caplog):
    # Written before add --direct refused it: kept, as every direct site is, and named.
    host = _host(tmp_path, monkeypatch)
    write_site_block("loop", generate_direct_site_block("loop", "i-a-m.live", target_port=7003), conf_dir=host.conf)
    assert main(["caddy-sync"]) == 0
    assert "loop: the direct site proxies to 127.0.0.1:7003, Caddy's direct listener (:7003), not to an app. " \
           "Remove it (webspec-ctl rm loop)" in caplog.text
    assert read_site(host.block("loop")) == Site("loop", "direct", upstream="127.0.0.1:7003")


# ── F26: the sockets of the installed unit, never the kernel's word ──


@pytest.mark.parametrize("layout,service,direct", [(DUAL, ["fd/3 fd/5"], ["fd/4 fd/6"]),
                                                     (IPV4_ONLY, ["fd/3"], ["fd/4"])])
def test_every_command_binds_what_the_unit_passes(tmp_path, monkeypatch, probes, layout, service, direct):
    host = _host(tmp_path, monkeypatch, layout=layout)
    assert main(["add", "notes", "--command", "python3", "--level", "1"]) == 0
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    assert main(["caddy-sync"]) == 0
    assert (_binds(host.block("notes")), _binds(host.block("web")), _binds(host.block("op-auth"))) == \
        (service, direct, service)


def test_a_unit_that_passes_other_sockets_stops_every_change(tmp_path, monkeypatch, probes, capsys):
    host = _host(tmp_path, monkeypatch)
    write_site_block("op-auth", caddy.generate_site_block("op-auth", "i-a-m.live", guard=True, listen=DUAL.gateway),
                     conf_dir=host.conf)
    before = host.snapshot()
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: ("127.0.0.1:7001", "[::1]:7001"))  # fc0c558's unit
    for argv in (["add", "notes", "--command", "x"], ["add", "web", "--direct", "--port", "3000"],
                 ["caddy-sync"], ["rm", "op-auth"]):
        assert main(argv) == 1, argv
        assert "Run gateway/tools/setup-caddy.sh again" in capsys.readouterr().err
    assert host.snapshot() == before and host.caddy.calls() == []


# ── caddy-sync: direct sites that move, and a new domain ──


def test_sync_moves_an_old_direct_block_to_its_listener_and_prints_the_rules(tmp_path, monkeypatch, probes, capsys):
    host = _host(tmp_path, monkeypatch)
    # A trusted direct block from before direct sites had a listener of their own (fc0c558).
    old = generate_direct_site_block("web", "i-a-m.live", target_port=3000).replace(":7003", ":7001")
    write_site_block("web", old.replace("bind fd/4 fd/6", "bind fd/3 fd/5"), conf_dir=host.conf)
    assert read_site(host.block("web")) == Site("web", "direct", upstream="127.0.0.1:3000")
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert _addresses(host.block("web")) == "http://web.localhost:7003, http://web.i-a-m.live:7003 {"
    assert "web moved to the direct listener (127.0.0.1:7003 and [::1]:7003): web.localhost, web.i-a-m.live" in out
    assert "cloudflared must send their public hosts there" in out
    assert out.rstrip().splitlines()[-2:] == ["  - hostname: web.i-a-m.live", "    service: http://127.0.0.1:7003"]


def test_a_new_domain_says_to_restart_the_gateway(tmp_path, monkeypatch, probes, capsys):
    _host(tmp_path, monkeypatch)
    assert main(["add", "web", "--direct", "--port", "3000"]) == 0
    monkeypatch.setattr(caddy, "_system_unit_loaded", lambda unit: False)  # a development host
    monkeypatch.setenv("WEBSPEC_DOMAIN", "example.org")
    capsys.readouterr()
    assert main(["caddy-sync"]) == 0
    out = capsys.readouterr().out
    assert "The gateway reads WEBSPEC_DOMAIN only when it starts. Set WEBSPEC_DOMAIN=example.org in " in out
    assert "systemctl --user restart webspec-gateway" in out
    # web stays a direct site, on the new domain: nothing to say of a site it no longer is (P10).
    assert "web no longer serves web.i-a-m.live" in out and "was a direct site" not in out
    rules = out[out.index("ingress:"):].splitlines()
    assert rules[:5] == ["ingress:", "  - hostname: web.example.org", "    service: http://127.0.0.1:7003",
                         '  - hostname: "*.example.org"', "    service: http://127.0.0.1:7001"]


def test_an_unchanged_domain_needs_no_restart(tmp_path, monkeypatch, probes, capsys):
    _host(tmp_path, monkeypatch)
    monkeypatch.setattr(caddy, "_system_unit_loaded", lambda unit: pytest.fail("no hint needed"))
    assert main(["caddy-sync"]) == 0
    assert "restart" not in capsys.readouterr().out
