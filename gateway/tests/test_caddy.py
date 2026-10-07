"""Tests for caddy: site blocks, the global Caddyfile, conf.d trust and sync, reload, deploy files."""

import json
import os
import re
import shutil
import stat
import subprocess
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from webspec import caddy
from webspec.caddy import (
    DUAL,
    IPV4_ONLY,
    Site,
    generate_direct_site_block,
    generate_global_caddyfile,
    generate_site_block,
    plan_sites,
    public_domain,
    read_site,
    reload_caddy,
    remove_site_block,
    scan_conf_dir,
    sync_caddy_config,
    write_site_block,
)

GATEWAY_DIR = Path(__file__).resolve().parents[1]
UNIT_FILE = GATEWAY_DIR / "deploy" / "linux" / "caddy-webspec.service"
SOCKET_FILE = GATEWAY_DIR / "deploy" / "linux" / "caddy-webspec.socket"
SETUP_SCRIPT = GATEWAY_DIR / "tools" / "setup-caddy.sh"
LIVE_CADDYFILE = caddy.CADDYFILE  # before _no_live_host_files points it elsewhere


@pytest.fixture(autouse=True)
def _host_has_ipv6(monkeypatch, tmp_path):
    """Generated text must not depend on the test host: both families, unless a test says not."""
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: True)
    # The socket unit as systemd would report it, never this machine's.
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: DUAL.addresses)
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", tmp_path / "no-drop-in" / "10-ipv4-only.conf")


@pytest.fixture(autouse=True)
def _no_live_host_files(tmp_path, monkeypatch):
    """Nothing reads this machine's /etc/caddy/Caddyfile or /etc/webspec, or its WEBSPEC_DOMAIN."""
    from webspec import config_writer
    monkeypatch.setattr(caddy, "CADDYFILE", tmp_path / "no-etc-caddy" / "Caddyfile")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "no-etc-webspec" / "config.json")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "no-etc-webspec" / "gateway.env")
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)
    monkeypatch.setattr(caddy, "_on_macos", lambda: False)  # these are the Linux deployment's files


def _all_blocks():
    return {
        "guarded": generate_site_block("svc", "i-a-m.live", guard=True),
        "unguarded": generate_site_block("open", "i-a-m.live", guard=False),
        "direct": generate_direct_site_block("web", "i-a-m.live", target_port=3000),
    }


def _section(text: str, opener: str) -> str:
    """Return the brace-balanced block that starts at the first line equal to `opener` (stripped)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == opener)
    depth, out = 0, []
    for line in lines[start:]:
        out.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(out)


def _assert_filtered(log_block: str) -> None:
    assert "format filter {" in log_block
    assert "wrap json" in log_block
    assert 'request>uri regexp ^[A-Za-z][A-Za-z0-9+.-]*://[^/?]*|^[^/?]*@|[?].* ""' in log_block
    assert "request>headers delete" in log_block
    assert "resp_headers delete" in log_block
    assert not re.search(r"^\s*format json\s*$", log_block, re.M), "unfiltered JSON encoder"


def _services(**guards):
    return {name: SimpleNamespace(guard=guard) for name, guard in guards.items()}


# ── generate_site_block ──


def test_generate_site_block_basic():
    block = generate_site_block("supabase", "i-a-m.live", guard=True)
    assert "http://supabase.localhost:7001" in block
    assert "http://supabase.i-a-m.live:7001" in block
    assert "reverse_proxy 127.0.0.1:7002" in block
    assert "/var/log/caddy/supabase.log" in block
    assert "format filter" in block


def test_generate_site_block_custom_ports():
    block = generate_site_block("svc", "example.com", gateway_port=9000, caddy_port=8080, guard=True)
    assert "http://svc.localhost:8080" in block
    assert "http://svc.example.com:8080" in block
    assert "reverse_proxy 127.0.0.1:9000" in block


def test_gateway_upstream_is_the_ipv4_loopback_not_localhost():
    # The gateway binds 127.0.0.1; dialing "localhost" may try ::1 first, where any local
    # process could be listening on the same port.
    for block in _all_blocks().values():
        assert "reverse_proxy localhost" not in block


def test_rate_limit_counts_tunnel_clients_apart_from_local_requests():
    block = generate_site_block("svc", "i-a-m.live", guard=True)
    rl = _section(block, "rate_limit {")
    tunnel, local = _section(rl, "zone svc_tunnel {"), _section(rl, "zone svc_local {")
    assert "header Cf-Ray *" in _section(tunnel, "match {")
    # Tunneled clients are counted by the key the map derives from Cf-Connecting-Ip (F28).
    assert "key {webspec_client}" in tunnel
    assert "\tmap {http.request.header.Cf-Connecting-Ip} {webspec_client} {\n" in block
    assert "not header Cf-Ray *" in _section(local, "match {")
    assert "key {remote_host}" in local
    for zone in (tunnel, local):
        assert "events 60" in zone and "window 1m" in zone


def test_rate_limit_zone_names_cannot_collide_across_services():
    # Zones are global in caddy-ratelimit; "_" never occurs in a service name (a DNS label).
    names = re.findall(r"zone (\S+) \{", generate_site_block("a", "x.dev") + generate_site_block("a-local", "x.dev"))
    assert len(names) == len(set(names)) == 8
    assert all("_" in n and caddy.is_label(n.split("_", 1)[0]) for n in names)


def test_generate_site_block_custom_rate_limit():
    block = generate_site_block("svc", "i-a-m.live", rate_limit=120, rate_window="2m")
    assert block.count("events 120") == 2
    assert block.count("events 240") == 2  # the nonce zones: twice as much
    assert block.count("window 2m") == 4
    block = generate_site_block("svc", "i-a-m.live", rate_limit=120, nonce_rate_limit=300)
    assert block.count("events 300") == 2


def test_generate_site_block_non_guard_has_logging():
    block = generate_site_block("open-svc", "i-a-m.live", guard=False)
    assert "log {" in block
    assert "format filter" in block


def test_no_path_is_exempt_from_rate_limiting():
    # F28: /__nonce and /__challenge have zones of their own; every request falls in exactly one.
    block = generate_site_block("svc", "i-a-m.live")
    assert "@notHealth" not in block
    assert "\trate_limit {\n" in block  # no matcher on the handler: every request reaches a zone
    rl = _section(block, "rate_limit {")
    matchers = {zone: _section(rl, f"zone svc_{zone} {{") for zone in ("tunnel", "nonce_tunnel", "local", "nonce_local")}
    nonce = "path /__nonce /__challenge"
    assert f"not {nonce}" in matchers["tunnel"] and f"not {nonce}" in matchers["local"]
    for zone in ("nonce_tunnel", "nonce_local"):
        assert f"\t{nonce}\n" in matchers[zone] and "events 120" in matchers[zone]
    assert "header Cf-Ray *" in matchers["nonce_tunnel"] and "not header Cf-Ray *" in matchers["nonce_local"]


def test_reverse_proxy_keeps_the_host_header():
    # DP-5: the guard signs Host. reverse_proxy passes it through unless told otherwise, so the
    # directive must stay bare: an upstream and nothing else (no header_up, no block).
    for block in _all_blocks().values():
        directives = [line.split() for line in block.splitlines() if line.strip().startswith("reverse_proxy ")]
        assert len(directives) == 1
        assert len(directives[0]) == 2, directives[0]
        assert "header_up" not in block


def test_unguarded_block_has_no_public_host():
    block = generate_site_block(name="mail-proton", domain="i-a-m.live", guard=False)
    assert "mail-proton.localhost" in block
    assert "mail-proton.i-a-m.live" not in block


def test_guarded_block_has_public_host():
    block = generate_site_block(name="op-auth", domain="i-a-m.live", guard=True)
    assert "op-auth.localhost" in block
    assert "op-auth.i-a-m.live" in block


def test_empty_domain_means_no_public_host():
    block = generate_site_block("svc", "", guard=True)
    assert "http://svc.localhost:7001 {" in block
    assert "http://svc.:" not in block


# ── generate_direct_site_block ──


def test_generate_direct_site_block_basic():
    block = generate_direct_site_block("llms", "i-a-m.live", target_port=20128)
    # DP-3: on the direct listener, never on the gateway's (:7001), which the agent may reach.
    assert "http://llms.localhost:7003, http://llms.i-a-m.live:7003 {" in block
    assert not re.search(r"http://\S*:7001", block) and "fd/3" not in block and "fd/5" not in block
    assert "reverse_proxy 127.0.0.1:20128" in block
    assert "/var/log/caddy/llms.log" in block
    assert "rate_limit" not in block
    assert "__nonce" not in block


def test_generate_direct_site_block_custom_caddy_port():
    block = generate_direct_site_block("app", "example.com", target_port=3000, caddy_port=8080)
    assert "http://app.localhost:8080" in block
    assert "http://app.example.com:8080" in block
    assert "reverse_proxy 127.0.0.1:3000" in block


def test_direct_site_dials_an_explicit_ipv6_loopback():
    block = generate_direct_site_block("app", "example.com", target_port=3000, target_host="::1")
    assert "reverse_proxy [::1]:3000" in block
    assert generate_direct_site_block("app", "example.com", target_port=3000, target_host="[::1]") == block


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "::", "192.168.1.10", "example.com", "127.0.0.1:80", ""])
def test_direct_site_refuses_names_and_non_loopback_targets(host):
    # A name may resolve to ::1 first, where any local user can listen; Caddy reaches loopback only.
    with pytest.raises(ValueError):
        generate_direct_site_block("app", "example.com", target_port=3000, target_host=host)


# ── DP-9: Caddy binds only systemd's loopback sockets (C2: services fd 3+5, direct sites fd 4+6) ──


@pytest.mark.parametrize("kind,binds", [("guarded", "fd/3 fd/5"), ("unguarded", "fd/3 fd/5"), ("direct", "fd/4 fd/6")])
def test_every_site_block_binds_only_its_listeners_fds(kind, binds):
    block = _all_blocks()[kind]
    assert f"\tbind {binds}\n" in block
    assert block.count("\tbind ") == 1


def test_without_ipv6_the_ipv4_fds_stay_where_they_are(monkeypatch):
    # On a kernel without IPv6 systemd ignores the [::1] lines: fd 3 (:7001) and fd 4 (:7003) stay.
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: IPV4_ONLY.addresses)
    assert "\tbind fd/3\n" in generate_site_block("svc", "i-a-m.live", guard=True)
    assert "\tbind fd/4\n" in generate_direct_site_block("web", "i-a-m.live", target_port=3000)
    text = generate_global_caddyfile()
    assert "default_bind fd/3\n" in text
    assert "fd/5" not in text and "fd/6" not in text
    assert "\tbind fd/3\n" in _section(text, "http://:7001 {") and "\tbind fd/4\n" in _section(text, "http://:7003 {")


def test_ipv6_probe_reports_false_when_the_kernel_has_no_ipv6(monkeypatch):
    monkeypatch.undo()  # the real probe, with a socket() that fails as without IPv6

    def no_ipv6(*a, **kw):
        raise OSError(97, "Address family not supported by protocol")

    monkeypatch.setattr(caddy.socket, "socket", no_ipv6)
    assert caddy.ipv6_supported() is False
    assert caddy.planned_listeners() == IPV4_ONLY


def test_explicit_fds_are_used():
    assert "\tbind fd/3\n" in generate_site_block("svc", "i-a-m.live", listen=(3,))
    assert "\tbind fd/6\n" in generate_direct_site_block("web", "i-a-m.live", target_port=3000, listen=(6,))


@pytest.mark.parametrize("bad", [(), (0,), (4,), (6,), (7,), (3, 3), ("3",), (True,)])
def test_service_blocks_bind_the_gateway_listener_only(bad):
    with pytest.raises(ValueError):
        generate_site_block("svc", "i-a-m.live", listen=bad)


@pytest.mark.parametrize("bad", [(), (3,), (5,), (4, 4), (True,)])
def test_direct_blocks_bind_the_direct_listener_only(bad):
    with pytest.raises(ValueError):
        generate_direct_site_block("web", "i-a-m.live", target_port=3000, listen=bad)


@pytest.mark.parametrize("bad", [(3,), (3, 4), "dual", caddy.Listeners("dual", (3, 4), (5, 6), ())])
def test_the_caddyfile_takes_a_layout_of_the_socket_unit(bad):
    with pytest.raises(ValueError):
        generate_global_caddyfile(listen=bad)


def test_generated_text_never_binds_an_address():
    for text in [generate_global_caddyfile(), *_all_blocks().values()]:
        for line in text.splitlines():
            if re.match(r"\s*(default_)?bind ", line):
                assert all(arg.startswith("fd/") for arg in line.split()[1:]), line


# ── DP-5: a loopback host through the tunnel ──


@pytest.mark.parametrize("kind,name", [("guarded", "svc"), ("unguarded", "open"), ("direct", "web")])
def test_tunneled_request_for_a_loopback_host_is_refused(kind, name):
    block = _all_blocks()[kind]
    matcher = _section(block, "@tunneled_loopback {")
    assert f"host {name}.localhost" in matcher
    assert "header Cf-Ray *" in matcher
    assert re.search(r'respond @tunneled_loopback "[^"]+" 421', block)


# ── DP-6: filtered logs ──


@pytest.mark.parametrize("kind", ["guarded", "unguarded", "direct"])
def test_access_log_drops_query_strings_and_headers(kind):
    _assert_filtered(_section(_all_blocks()[kind], "log {"))


@pytest.mark.parametrize("uri,logged", [
    ("/send_email?to=a%40b.c&secret=hunter2", "/send_email"),
    ("/x", "/x"),
    ("/a@b/c?d=1", "/a@b/c"),  # an @ in a path is path
    # F25: an absolute-form target loses its scheme, authority and userinfo, whatever the case.
    ("http://svc.localhost:7001/x?k=v", "/x"),
    ("http://user:SECRET@svc.localhost/tool?arg=1", "/tool"),
    ("HTTPS://u:SECRET@svc.localhost:7001/a/b?x", "/a/b"),
    ("hTTp://u:SECRET@svc.localhost", ""),
    # A CONNECT target (authority-form) loses its userinfo.
    ("u:SECRET@svc.localhost:7001", "svc.localhost:7001"),
    ("svc.localhost:7001", "svc.localhost:7001"),
    ("*", "*"),
])
def test_uri_filter_keeps_the_path_only(uri, logged):
    # Caddy uses Go's RE2 (leftmost-first, like Python's re for this pattern); the real Caddy run
    # below and tests/test_caddy_limits.py check the same with Caddy itself.
    pattern = re.search(r'request>uri regexp (\S+) ""', caddy.LOG_FILTER_FIELDS[0]).group(1)
    assert pattern == caddy.URI_FILTER
    assert re.sub(pattern, "", uri) == logged


def test_log_dir_can_be_staged(tmp_path):
    block = generate_site_block("svc", "i-a-m.live", log_dir=tmp_path / "log")
    assert f"output file {tmp_path}/log/svc.log {{" in block
    assert "/var/log/caddy" not in block


# ── generate_global_caddyfile ──


def test_global_caddyfile_options():
    text = generate_global_caddyfile()
    assert text.startswith(caddy.GENERATED_MARKER)
    options = _section(text, "{")
    assert "\tdefault_bind fd/3 fd/5\n" in options
    assert "\tadmin unix//run/caddy-webspec/admin.sock|0200\n" in options
    assert "\tauto_https off\n" in options
    assert "\tpersist_config off\n" in options
    assert "protocols h1" in options


def test_global_caddyfile_admin_api_is_not_on_tcp():
    text = generate_global_caddyfile()
    assert "2019" not in text
    assert "admin localhost" not in text
    admin = re.search(r"^\tadmin (\S+)$", text, re.M).group(1)
    assert admin.startswith("unix//")
    path, mode = admin[len("unix/"):].split("|")
    assert Path(path) == caddy.CADDY_ADMIN_SOCKET
    assert int(mode, 8) & 0o077 == 0, "group/other must not reach the admin socket"


def test_global_caddyfile_default_logger_is_filtered():
    # Error entries (a 502 while the gateway is down) carry the request and go to the default log.
    options = _section(generate_global_caddyfile(), "{")
    log = _section(options, "log {")
    assert "output stderr" in log
    _assert_filtered(log)


def test_global_caddyfile_imports_site_blocks_and_ends_in_catch_alls():
    text = generate_global_caddyfile()
    assert "import /etc/caddy/conf.d/*.caddy" in text
    # One catch-all per listener (C2): the gateway's and the direct one.
    for opener, binds in (("http://:7001 {", "fd/3 fd/5"), ("http://:7003 {", "fd/4 fd/6")):
        catch_all = _section(text, opener)
        assert f"\tbind {binds}\n" in catch_all
        assert 'respond "Unknown service" 421' in catch_all
        assert "reverse_proxy" not in catch_all
        assert text.index("import ") < text.index(opener)


def test_global_caddyfile_custom_port_and_paths(tmp_path):
    text = generate_global_caddyfile(caddy_port=8080, conf_dir=tmp_path / "conf.d",
                                     admin_socket=tmp_path / "admin.sock")
    assert f"import {tmp_path}/conf.d/*.caddy" in text
    assert f"admin unix/{tmp_path}/admin.sock|0200" in text
    assert "http://:8080 {" in text and "http://:7003 {" in text


@pytest.mark.parametrize("bad", ["run/admin.sock", "/run/a b.sock", "/run/x|0777", "/run/{x}"])
def test_global_caddyfile_rejects_unsafe_paths(bad):
    with pytest.raises(ValueError):
        generate_global_caddyfile(admin_socket=Path(bad))


def test_generated_text_has_balanced_braces():
    for text in [generate_global_caddyfile(), *_all_blocks().values()]:
        depth = 0
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            depth += line.count("{") - line.count("}")
            assert depth >= 0
        assert depth == 0


def test_every_generated_file_carries_the_marker():
    for text in [generate_global_caddyfile(), *_all_blocks().values()]:
        assert text.startswith(caddy.GENERATED_MARKER)


# ── input validation ──


@pytest.mark.parametrize("name", ["", "Svc", "a b", "x{", "svc.evil", "-svc", "a" * 64])
def test_site_block_rejects_names_that_are_not_labels(name):
    with pytest.raises(ValueError):
        generate_site_block(name, "i-a-m.live")
    with pytest.raises(ValueError):
        generate_direct_site_block(name, "i-a-m.live", target_port=3000)


@pytest.mark.parametrize("domain", ["exa mple.com", "evil.com\n}", "Example.com", "a..b"])
def test_site_block_rejects_bad_domains(domain):
    with pytest.raises(ValueError):
        generate_site_block("svc", domain, guard=True)


@pytest.mark.parametrize("kwargs", [{"gateway_port": 0}, {"caddy_port": 70000}, {"caddy_port": "7001"},
                                    {"rate_limit": 0}, {"rate_window": "1m; respond 200"}])
def test_site_block_rejects_bad_numbers(kwargs):
    with pytest.raises(ValueError):
        generate_site_block("svc", "i-a-m.live", **kwargs)


# ── metadata and trust ──


def test_every_site_block_records_what_it_serves():
    assert read_site_text(_all_blocks()["guarded"]) == Site("svc", "service", guard=True)
    assert read_site_text(_all_blocks()["unguarded"]) == Site("open", "service", guard=False)
    assert read_site_text(_all_blocks()["direct"]) == Site("web", "direct", upstream="127.0.0.1:3000")


def read_site_text(text: str) -> Site:
    line = text.splitlines()[1]
    assert line.startswith(caddy.SITE_META_PREFIX)
    return Site.from_meta(json.loads(line[len(caddy.SITE_META_PREFIX):]))


def test_render_site_round_trips():
    for name, text in [("svc", _all_blocks()["guarded"]), ("web", _all_blocks()["direct"])]:
        assert caddy.render_site(read_site_text(text), "i-a-m.live") == text


@pytest.mark.parametrize("meta", [
    {"kind": "service", "name": "svc", "guard": "yes"},
    {"kind": "service", "name": "svc", "upstream": "127.0.0.1:1"},
    {"kind": "direct", "name": "web", "upstream": "10.0.0.1:80"},
    {"kind": "direct", "name": "web", "upstream": "localhost:80"},
    {"kind": "direct", "name": "web", "upstream": "127.0.0.1"},
    {"kind": "direct", "name": "web"},
    {"kind": "proxy", "name": "web"},
    {"kind": "service", "name": "Bad Name"},
    {"kind": "service", "name": "svc", "extra": 1},
    ["svc"],
])
def test_bad_metadata_is_refused(meta):
    with pytest.raises((ValueError, TypeError)):
        Site.from_meta(meta)


def test_a_generated_block_written_by_us_is_trusted(tmp_path):
    write_site_block("svc", _all_blocks()["guarded"], conf_dir=tmp_path)
    write_site_block("web", _all_blocks()["direct"], conf_dir=tmp_path)
    trusted, untrusted = scan_conf_dir(tmp_path)
    assert trusted == {"svc": Site("svc", "service", guard=True),
                       "web": Site("web", "direct", upstream="127.0.0.1:3000")}
    assert untrusted == []


def _untrusted(tmp_path, name="svc"):
    trusted, untrusted = scan_conf_dir(tmp_path)
    assert name not in trusted
    assert tmp_path / f"{name}.caddy" in untrusted


def test_an_unmarked_block_is_not_trusted(tmp_path):
    (tmp_path / "svc.caddy").write_text("http://svc.localhost:7001 {\n\treverse_proxy localhost:7002\n}\n")
    _untrusted(tmp_path)


def test_a_block_others_can_write_is_not_trusted(tmp_path):
    p = write_site_block("svc", _all_blocks()["guarded"], conf_dir=tmp_path)
    p.chmod(0o664)
    _untrusted(tmp_path)


def test_a_block_owned_by_another_user_is_not_trusted(tmp_path, monkeypatch):
    write_site_block("svc", _all_blocks()["guarded"], conf_dir=tmp_path)
    monkeypatch.setattr(caddy, "_trusted_uids", lambda: frozenset({os.geteuid() + 1}))
    _untrusted(tmp_path)


def test_a_symlinked_block_is_not_trusted(tmp_path):
    real = tmp_path / "elsewhere.txt"
    real.write_text(_all_blocks()["guarded"])
    (tmp_path / "svc.caddy").symlink_to(real)
    _untrusted(tmp_path)


def test_a_fifo_or_directory_named_like_a_block_is_not_trusted_and_does_not_hang(tmp_path):
    os.mkfifo(tmp_path / "svc.caddy")
    (tmp_path / "dir.caddy").mkdir()
    trusted, untrusted = scan_conf_dir(tmp_path)
    assert trusted == {}
    assert sorted(p.name for p in untrusted) == ["dir.caddy", "svc.caddy"]


def test_a_block_whose_metadata_names_another_site_is_not_trusted(tmp_path):
    (tmp_path / "other.caddy").write_text(_all_blocks()["guarded"])  # metadata says "svc"
    _untrusted(tmp_path, "other")


def test_files_outside_the_import_glob_are_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".svc.caddy.abc.tmp").write_text("x")
    assert scan_conf_dir(tmp_path) == ({}, [])


# ── write_site_block / remove_site_block ──


def test_write_site_block(tmp_path):
    content = "test block content"
    p = write_site_block("my-svc", content, conf_dir=tmp_path)
    assert p == tmp_path / "my-svc.caddy"
    assert p.read_text() == content
    assert [q.name for q in tmp_path.iterdir()] == ["my-svc.caddy"]  # no temporary file left


def test_write_site_block_is_world_readable_under_a_strict_umask(tmp_path):
    # Written by root, read by Caddy running as the caddy user.
    old = os.umask(0o077)
    try:
        p = write_site_block("my-svc", "x", conf_dir=tmp_path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_write_site_block_replaces_a_symlink_without_touching_its_target(tmp_path):
    target = tmp_path / "precious.txt"
    target.write_text("precious")
    target.chmod(0o600)
    conf = tmp_path / "conf.d"
    conf.mkdir()
    (conf / "svc.caddy").symlink_to(target)
    p = write_site_block("svc", "block", conf_dir=conf)
    assert not p.is_symlink() and p.read_text() == "block"
    assert target.read_text() == "precious"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_site_block_writes_a_new_file_instead_of_reusing_the_old_one(tmp_path):
    # A file someone else owns (or has hard-linked) must not survive with its owner or links.
    old = tmp_path / "svc.caddy"
    old.write_text("old")
    link = tmp_path / "hardlink"
    os.link(old, link)
    write_site_block("svc", "new", conf_dir=tmp_path)
    assert old.read_text() == "new"
    assert link.read_text() == "old"
    assert os.stat(old).st_ino != os.stat(link).st_ino


def test_write_site_block_refuses_a_bad_name(tmp_path):
    with pytest.raises(ValueError):
        write_site_block("../evil", "x", conf_dir=tmp_path)


def test_remove_site_block(tmp_path):
    (tmp_path / "my-svc.caddy").write_text("content")
    remove_site_block("my-svc", conf_dir=tmp_path)
    assert not (tmp_path / "my-svc.caddy").exists()


def test_remove_site_block_removes_a_dangling_symlink(tmp_path):
    (tmp_path / "my-svc.caddy").symlink_to(tmp_path / "missing")
    remove_site_block("my-svc", conf_dir=tmp_path)
    assert not (tmp_path / "my-svc.caddy").is_symlink()


def test_remove_site_block_missing_noop(tmp_path):
    remove_site_block("nonexistent", conf_dir=tmp_path)  # no error


# ── sync_caddy_config ──


def _mock_registry(services: dict):
    """Create a mock ServiceRegistry with given service names and guard flags."""
    registry = MagicMock()
    registry.names.return_value = list(services.keys())

    def mock_get(name):
        if name not in services:
            return None
        entry = MagicMock()
        entry.guard = services[name]
        return entry

    registry.get.side_effect = mock_get
    return registry


def test_sync_adds_missing(tmp_path):
    registry = _mock_registry({"svc-a": False, "svc-b": True})
    added, removed = sync_caddy_config(registry, "i-a-m.live", conf_dir=tmp_path)
    assert added == ["svc-a", "svc-b"]
    assert removed == []
    assert read_site(tmp_path / "svc-a.caddy") == Site("svc-a", "service", guard=False)
    assert read_site(tmp_path / "svc-b.caddy") == Site("svc-b", "service", guard=True)


def test_sync_removes_a_stale_service_block_it_wrote(tmp_path):
    sync_caddy_config(_mock_registry({"old-svc": True, "new-svc": False}), "i-a-m.live", conf_dir=tmp_path)
    added, removed = sync_caddy_config(_mock_registry({"new-svc": False}), "i-a-m.live", conf_dir=tmp_path)
    assert added == []
    assert removed == ["old-svc"]
    assert not (tmp_path / "old-svc.caddy").exists()


def test_sync_leaves_files_it_did_not_write(tmp_path, caplog):
    # An unmarked block may be a hand-made site; setup-caddy.sh moves such files out.
    (tmp_path / "legacy.caddy").write_text("stale")
    added, removed = sync_caddy_config(_mock_registry({"new-svc": False}), "i-a-m.live", conf_dir=tmp_path)
    assert added == ["new-svc"]
    assert removed == []
    assert (tmp_path / "legacy.caddy").read_text() == "stale"
    assert "legacy.caddy" in caplog.text


def test_sync_keeps_and_regenerates_direct_sites(tmp_path):
    outdated = _all_blocks()["direct"].replace("reverse_proxy 127.0.0.1:3000", "reverse_proxy localhost:3000")
    write_site_block("web", outdated, conf_dir=tmp_path)
    added, removed = sync_caddy_config(_mock_registry({"svc": True}), "i-a-m.live", conf_dir=tmp_path)
    assert (added, removed) == (["svc"], [])
    assert (tmp_path / "web.caddy").read_text() == _all_blocks()["direct"]


@pytest.mark.parametrize("registry", [_mock_registry({}), {}])
def test_sync_with_no_services_removes_nothing(tmp_path, registry, caplog):
    # A missing or unreadable config reads as an empty registry: it must never wipe the proxy.
    sync_caddy_config(_mock_registry({"svc": True, "open": False}), "i-a-m.live", conf_dir=tmp_path)
    write_site_block("web", _all_blocks()["direct"], conf_dir=tmp_path)
    added, removed = sync_caddy_config(registry, "i-a-m.live", conf_dir=tmp_path)
    assert (added, removed) == ([], [])
    assert sorted(p.name for p in tmp_path.glob("*.caddy")) == ["open.caddy", "svc.caddy", "web.caddy"]
    assert "lists no services" in caplog.text


def test_sync_lets_a_service_replace_a_direct_site_of_the_same_name(tmp_path, caplog):
    write_site_block("svc", _all_blocks()["direct"].replace("'web'", "'svc'").replace('"web"', '"svc"'),
                     conf_dir=tmp_path)
    sync_caddy_config(_mock_registry({"svc": True}), "i-a-m.live", conf_dir=tmp_path)
    assert read_site(tmp_path / "svc.caddy") == Site("svc", "service", guard=True)
    assert "replaces the direct site" in caplog.text


def test_sync_updates_existing(tmp_path):
    (tmp_path / "svc.caddy").write_text("old content")
    added, removed = sync_caddy_config(_mock_registry({"svc": False}), "i-a-m.live", conf_dir=tmp_path)
    # Already existed, so not in "added"
    assert "svc" not in added
    assert removed == []
    # But content updated
    new_content = (tmp_path / "svc.caddy").read_text()
    assert "reverse_proxy" in new_content
    assert "\tbind fd/3 fd/5" in new_content


def test_sync_domain_and_port_customization(tmp_path):
    sync_caddy_config(_mock_registry({"svc": True}), "custom.dev", gateway_port=9999, caddy_port=8080,
                      conf_dir=tmp_path)
    content = (tmp_path / "svc.caddy").read_text()
    assert "http://svc.custom.dev:8080" in content
    assert "reverse_proxy 127.0.0.1:9999" in content


def test_sync_skips_a_name_it_cannot_serve(tmp_path):
    long_name = "x" * 70  # normalize_name does not cap length; the gateway rejects it anyway
    added, _ = sync_caddy_config(_mock_registry({"good": True, long_name: True}), "i-a-m.live", conf_dir=tmp_path)
    assert added == ["good"]
    assert not (tmp_path / f"{long_name}.caddy").exists()


def test_sync_accepts_a_mapping_of_services(tmp_path):
    added, _ = sync_caddy_config(_services(svc=True), "i-a-m.live", conf_dir=tmp_path)
    assert added == ["svc"]


def test_plan_keeps_service_blocks_when_there_is_no_config(tmp_path):
    sync_caddy_config(_mock_registry({"svc": True}), "i-a-m.live", conf_dir=tmp_path)
    plan = plan_sites(None, tmp_path)
    assert plan.sites == {"svc": Site("svc", "service", guard=True)} and plan.stale == []


# ── public domain ──


def test_domain_from_the_environment_wins_even_when_empty(tmp_path):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("WEBSPEC_DOMAIN=from-file.dev\n")
    assert public_domain({"WEBSPEC_DOMAIN": "env.dev"}, env_file) == ("env.dev", "the environment")
    assert public_domain({"WEBSPEC_DOMAIN": ""}, env_file) == ("", "the environment")


@pytest.mark.parametrize("sourced", [True, False])  # the macOS gateway.env (sh), the Linux one (systemd)
@pytest.mark.parametrize("text,expected", [
    ("WEBSPEC_DOMAIN=example.com\n", "example.com"),
    ('WEBSPEC_DOMAIN="example.com"\n', "example.com"),
    ("  WEBSPEC_DOMAIN='example.com'  \n", "example.com"),
    ("WEBSPEC_DOMAIN=old.dev\nWEBSPEC_GUARD_KEY=secret\nWEBSPEC_DOMAIN=new.dev\n", "new.dev"),
])
def test_domain_from_a_production_gateway_env(tmp_path, monkeypatch, sourced, text, expected):
    monkeypatch.setattr(caddy.config_writer, "env_file_sourced", lambda path=None: sourced)
    env_file = tmp_path / "gateway.env"
    env_file.write_text(text)
    assert public_domain({}, env_file, production=True) == (expected, str(env_file))


def test_an_export_line_sets_the_domain_where_sh_reads_the_file_only(tmp_path, monkeypatch):
    # The macOS gateway.env is sourced by sh; the Linux one is a systemd EnvironmentFile=, which
    # ignores `export NAME=value`, so the gateway there never gets the domain (P11).
    env_file = tmp_path / "gateway.env"
    env_file.write_text("export WEBSPEC_DOMAIN=example.com\n")
    monkeypatch.setattr(caddy.config_writer, "env_file_sourced", lambda path=None: True)
    assert public_domain({}, env_file, production=True) == ("example.com", str(env_file))
    monkeypatch.setattr(caddy.config_writer, "env_file_sourced", lambda path=None: False)
    assert public_domain({}, env_file, production=True) == ("", "not set")


@pytest.mark.parametrize("text", ["#WEBSPEC_DOMAIN=example.com\n", "WEBSPEC_GUARD_KEY=x\n", "",
                                  "# installer default: empty means loopback names only\nWEBSPEC_DOMAIN=\n"])
def test_no_domain_without_a_setting(tmp_path, text):
    # Never a built-in default: a domain this host does not serve must not reach its config.
    env_file = tmp_path / "gateway.env"
    env_file.write_text(text)
    assert public_domain({}, env_file, production=True) == ("", "not set")
    assert public_domain({}, tmp_path / "missing.env", production=True) == ("", "not set")


def test_gateway_env_counts_on_a_production_host_only(tmp_path):
    # Elsewhere /etc/webspec/gateway.env is not the env file of the gateway that runs.
    env_file = tmp_path / "gateway.env"
    env_file.write_text("WEBSPEC_DOMAIN=example.com\n")
    assert public_domain({}, env_file, production=False) == ("", "not set")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_gateway_env_sets_no_domain(tmp_path):
    env_file = tmp_path / "gateway.env"
    env_file.write_text("WEBSPEC_DOMAIN=example.com\n")
    env_file.chmod(0)
    assert public_domain({}, env_file, production=True) == ("", "not set")


# ── reload_caddy ──


def _completed(rc, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


@pytest.fixture
def installed_caddy(tmp_path, monkeypatch):
    """A file at CADDY_BIN, standing in for the caddy setup-caddy.sh installs."""
    binary = tmp_path / "usr-local-bin" / "caddy"
    binary.parent.mkdir()
    binary.write_text("")
    monkeypatch.setattr(caddy, "CADDY_BIN", binary)
    monkeypatch.delenv("WEBSPEC_CADDY_BIN", raising=False)
    monkeypatch.setattr(caddy, "_is_root", lambda: False)  # root would refuse a file under /tmp
    return binary


def test_reload_uses_the_config_for_the_admin_address(installed_caddy):
    # `caddy reload --config` reads admin.listen (the unix socket) from the config itself.
    with patch("webspec.caddy.subprocess.run", return_value=_completed(0)) as run:
        assert reload_caddy(Path("/etc/caddy/Caddyfile")) is True
    argv = run.call_args.args[0]
    assert argv == [str(installed_caddy), "reload", "--config", "/etc/caddy/Caddyfile"]
    assert "--address" not in argv


@pytest.mark.parametrize("root", [False, True])
def test_reload_never_runs_a_bare_caddy_from_path(tmp_path, monkeypatch, root):
    # No caddy at CADDY_BIN: nothing runs, as root least of all. The planted one on PATH (macOS
    # sudo keeps the caller's PATH) would otherwise run as root.
    bindir = tmp_path / "agent-bin"
    bindir.mkdir()
    marker = tmp_path / "ran"
    (bindir / "caddy").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    (bindir / "caddy").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(caddy, "CADDY_BIN", tmp_path / "missing" / "caddy")
    monkeypatch.delenv("WEBSPEC_CADDY_BIN", raising=False)
    monkeypatch.setattr(caddy, "_is_root", lambda: root)
    with patch("webspec.caddy.subprocess.run", wraps=subprocess.run) as run:
        assert reload_caddy() is False
    run.assert_not_called()
    assert not marker.exists()


def test_reload_prefers_the_installed_binary(installed_caddy, tmp_path):
    with patch("webspec.caddy.subprocess.run", return_value=_completed(0)) as run:
        reload_caddy(tmp_path / "Caddyfile")
    assert run.call_args.args[0] == [str(installed_caddy), "reload", "--config", str(tmp_path / "Caddyfile")]


def test_reload_runs_an_explicit_caddy_by_absolute_path(installed_caddy, tmp_path, monkeypatch):
    other = tmp_path / "opt-caddy"
    other.write_text("")
    monkeypatch.setenv("WEBSPEC_CADDY_BIN", str(other))
    with patch("webspec.caddy.subprocess.run", return_value=_completed(0)) as run:
        assert reload_caddy() is True
    assert run.call_args.args[0][0] == str(other)


def test_reload_failure_explains_the_admin_socket(installed_caddy, caplog):
    err = 'dial unix /run/caddy-webspec/admin.sock: connect: permission denied'
    with patch("webspec.caddy.subprocess.run", return_value=_completed(1, err)):
        assert reload_caddy() is False
    assert "sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync" in caplog.text


@pytest.mark.parametrize("exc", [FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("caddy", 10)])
def test_reload_reports_false_on_errors(installed_caddy, exc):
    with patch("webspec.caddy.subprocess.run", side_effect=exc) as run:
        assert reload_caddy() is False
    run.assert_called_once()


# ── the command line setup-caddy.sh runs ──


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Live paths under tmp_path, and a gateway config with a guarded and an unguarded service."""
    etc = tmp_path / "etc" / "caddy"
    monkeypatch.setattr(caddy, "CADDYFILE", etc / "Caddyfile")
    monkeypatch.setattr(caddy, "CADDY_CONF_DIR", etc / "conf.d")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcpServers": {"svc": {"command": "x", "level": 1}, "Open_Svc": {"command": "x"}}}))
    etc.mkdir(parents=True)
    return SimpleNamespace(etc=etc, conf=etc / "conf.d", config=config, disabled=etc / "conf.d.disabled" / "ts")


def _main(*argv):
    return caddy.main([str(a) for a in argv])


def test_cli_stage_writes_a_self_contained_copy(live, tmp_path):
    out = tmp_path / "stage"
    assert _main("stage", "--config", live.config, "--domain", "example.com", "--listeners", "dual", "--out", out) == 0
    caddyfile = (out / "Caddyfile").read_text()
    assert f"import {out}/conf.d/*.caddy" in caddyfile
    assert sorted(p.name for p in (out / "conf.d").iterdir()) == ["open-svc.caddy", "svc.caddy"]
    svc = (out / "conf.d" / "svc.caddy").read_text()
    assert f"output file {out}/log/svc.log" in svc and "http://svc.example.com:7001" in svc
    assert not caddy.CADDYFILE.exists() and not live.conf.exists()  # nothing live is written


def test_cli_apply_writes_the_live_config_and_moves_untrusted_blocks_out(live, capsys):
    live.conf.mkdir()
    (live.conf / "llms.caddy").write_text(
        "http://llms.localhost:7001 {\n    reverse_proxy localhost:20128\n    log {\n        format json\n    }\n}\n")
    (live.conf / "svc.caddy").write_text("http://svc.localhost:7001 {\n\treverse_proxy localhost:7002\n}\n")
    (live.conf / "evil.caddy").symlink_to("/etc/passwd")
    # F9: what a symlinked block serves cannot be read, so it is set aside only when allowed.
    before = sorted(p.name for p in live.conf.iterdir())
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled) == caddy.SHRINK_REFUSED
    assert "evil.caddy' (not a regular UTF-8 file)" in capsys.readouterr().err
    assert sorted(p.name for p in live.conf.iterdir()) == before and not caddy.CADDYFILE.exists()
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled, "--allow-shrink") == 0
    # F30: the old direct site is regenerated, on the direct listener, rather than dropped.
    assert sorted(p.name for p in live.conf.iterdir()) == ["llms.caddy", "open-svc.caddy", "svc.caddy"]
    assert read_site(live.conf / "llms.caddy") == Site("llms", "direct", upstream="127.0.0.1:20128")
    assert "http://llms.localhost:7003, http://llms.example.com:7003 {" in (live.conf / "llms.caddy").read_text()
    assert sorted(p.name for p in live.disabled.iterdir()) == ["evil.caddy", "llms.caddy", "svc.caddy"]
    assert (live.disabled / "evil.caddy").is_symlink()
    assert stat.S_IMODE(live.disabled.stat().st_mode) == 0o700
    assert stat.S_IMODE(live.disabled.parent.stat().st_mode) == 0o700
    assert read_site(live.conf / "svc.caddy") == Site("svc", "service", guard=True)
    assert caddy.CADDYFILE.read_text() == generate_global_caddyfile(conf_dir=live.conf, domain="example.com",
                                                                   listen=DUAL)
    out = capsys.readouterr().out
    assert "'llms.caddy': regenerated as a direct site, on the direct listener (:7003)" in out
    assert "'svc.caddy': replaced by the generated block" in out
    assert "'evil.caddy': no longer served" in out


def test_cli_apply_refuses_a_symlinked_quarantine(live, tmp_path):
    live.conf.mkdir()
    (live.conf / "legacy.caddy").write_text("x")
    elsewhere = tmp_path / "agent-dir"
    elsewhere.mkdir()
    live.disabled.parent.symlink_to(elsewhere)
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled) == 2
    assert (live.conf / "legacy.caddy").exists() and list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("command", ["plan", "stage", "apply"])
def test_cli_refuses_a_missing_config_and_changes_nothing(live, tmp_path, command, capsys):
    # F9: a missing config never reads as "no services"; setup-caddy.sh asks for WEBSPEC_CONFIG.
    sync_caddy_config(_services(svc=True), "example.com", conf_dir=live.conf)
    before = (live.conf / "svc.caddy").read_text()
    extra = {"plan": [], "stage": ["--out", tmp_path / "stage"], "apply": ["--disabled", live.disabled]}[command]
    assert _main(command, "--config", tmp_path / "missing.json", "--domain", "example.com", "--listeners", "dual",
                 *extra) == 2
    assert "does not exist" in capsys.readouterr().err
    assert (live.conf / "svc.caddy").read_text() == before and not caddy.CADDYFILE.exists()


def test_cli_refuses_a_config_it_cannot_parse_and_changes_nothing(live, capsys):
    live.config.write_text("{not json")
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled) == 2
    assert not caddy.CADDYFILE.exists() and not live.conf.exists()
    assert "cannot parse" in capsys.readouterr().err


@pytest.mark.parametrize("domain", ["Example.com", "a b", "x}"])
def test_cli_refuses_a_bad_domain(live, domain, capsys):
    assert _main("apply", "--config", live.config, "--domain", domain, "--listeners", "dual",
                 "--disabled", live.disabled) == 2
    assert not caddy.CADDYFILE.exists()


def test_cli_domain_and_listeners(monkeypatch, capsys):
    monkeypatch.setenv("WEBSPEC_DOMAIN", "example.com")
    assert _main("domain") == 0 and _main("listeners") == 0
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: False)
    assert _main("listeners") == 0
    assert capsys.readouterr().out == "example.com\tthe environment\ndual\nipv4\n"


def test_cli_has_no_listen_fds_any_more(capsys):
    # An older setup-caddy.sh asks for listen-fds and reads "3 4" as both families: it must stop
    # (it says the generator predates it) rather than install units that do not match.
    with pytest.raises(SystemExit):
        _main("listen-fds")


def test_cli_domain_reuses_the_recorded_one(live, capsys):
    # A re-run of setup-caddy.sh without WEBSPEC_DOMAIN (sudo drops it) keeps the public domain.
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled) == 0
    capsys.readouterr()
    assert _main("domain") == 0
    assert capsys.readouterr().out == f"example.com\trecorded in {caddy.CADDYFILE}\n"


def test_cli_domain_refuses_an_ambiguous_one(live, tmp_path, monkeypatch, capsys):
    from webspec import config_writer
    assert _main("apply", "--config", live.config, "--domain", "example.com", "--listeners", "dual",
                 "--disabled", live.disabled) == 0
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "gateway.env").write_text("WEBSPEC_DOMAIN=other.dev\n")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "gateway.env")
    assert _main("domain") == 2
    assert "WEBSPEC_DOMAIN=other.dev" in capsys.readouterr().err


# ── shipped deploy files ──


def _unit(path=UNIT_FILE):
    settings: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, value = line.split("=", 1)
            settings.setdefault(key, []).append(value)
    return settings


def test_unit_runs_caddy_as_caddy_without_privileges():
    unit = _unit()
    assert unit["User"] == ["caddy"] and unit["Group"] == ["caddy"]
    assert unit["NoNewPrivileges"] == ["yes"]
    assert unit["CapabilityBoundingSet"] == [""]
    assert unit["AmbientCapabilities"] == [""]
    assert "CAP_" not in UNIT_FILE.read_text()
    assert unit["ProtectSystem"] == ["strict"]
    assert unit["ProtectHome"] == ["yes"]
    assert unit["PrivateTmp"] == ["yes"]
    assert unit["IPAddressDeny"] == ["any"]
    assert unit["IPAddressAllow"] == ["localhost"]


def test_unit_takes_its_sockets_from_the_socket_unit():
    unit = _unit()
    assert unit["Requires"] == ["caddy-webspec.socket"]
    assert "caddy-webspec.socket" in unit["After"][0].split()
    assert unit["Also"] == ["caddy-webspec.socket"]


def test_nothing_makes_systemd_give_the_port_up():
    # systemd fails a socket unit, closing its sockets, when the socket hits its trigger limit or
    # its service hits the start limit (5 starts in 10 s by default: a few manual restarts).
    unit, sock = _unit(), _unit(SOCKET_FILE)
    assert unit["StartLimitIntervalSec"] == ["0"]
    assert sock["TriggerLimitIntervalSec"] == ["0"]
    # Restarts stay throttled; a start that a connection triggers waits for the restart timer.
    assert unit["Restart"] == ["always"] and unit["RestartSec"] == ["3"]


def test_socket_unit_binds_loopback_in_the_fd_order_the_config_uses():
    sock = _unit(SOCKET_FILE)
    # systemd passes the sockets from fd 3 up, in ListenStream order (C2): the IPv4 ones first, so
    # that on a kernel without IPv6, where systemd ignores the [::1] lines, they stay fds 3 and 4.
    assert sock["ListenStream"] == ["127.0.0.1:7001", "127.0.0.1:7003", "[::1]:7001", "[::1]:7003"]
    assert tuple(sock["ListenStream"]) == DUAL.addresses
    assert DUAL.addresses[:2] == IPV4_ONLY.addresses
    for layout in (DUAL, IPV4_ONLY):
        for fd, address in enumerate(layout.addresses, start=3):
            assert fd in (layout.gateway if address.endswith(":7001") else layout.direct)
    assert sock["FreeBind"] == ["yes"]
    assert sock["ReusePort"] == ["no"]
    assert sock["IPAddressDeny"] == ["any"] and sock["IPAddressAllow"] == ["localhost"]
    assert "Accept" not in sock  # one Caddy receives the listening sockets


def test_unit_matches_the_generated_config():
    unit = _unit()
    assert unit["ExecStart"] == [f"{caddy.CADDY_BIN} run --config {LIVE_CADDYFILE}"]
    assert unit["ExecReload"] == [f"{caddy.CADDY_BIN} reload --config {LIVE_CADDYFILE} --force"]
    # The admin socket lives in the unit's RuntimeDirectory, which only caddy can open.
    assert caddy.CADDY_ADMIN_SOCKET.parent == Path("/run") / unit["RuntimeDirectory"][0]
    assert unit["RuntimeDirectoryMode"] == ["0700"]
    assert unit["LogsDirectory"] == [caddy.CADDY_LOG_DIR.name]
    writable = unit["ReadWritePaths"][0].split()
    assert str(caddy.CADDY_LOG_DIR) in writable
    assert "/var/lib/caddy-webspec" in writable
    env = dict(e.split("=", 1) for e in unit["Environment"])
    assert env["XDG_DATA_HOME"].startswith("/var/lib/caddy-webspec/")
    assert env["XDG_CONFIG_HOME"].startswith("/var/lib/caddy-webspec/")


def test_documented_reload_commands_survive_sudo():
    # sudo drops WEBSPEC_*: a bare `sudo webspec-ctl caddy-sync` in the docs would read root's
    # ~/.claude.json on a host without the production fallback.
    for path in (UNIT_FILE, SETUP_SCRIPT, GATEWAY_DIR / "webspec" / "caddy.py"):
        assert "sudo webspec-ctl" not in path.read_text(), path


def test_setup_script_parses():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    subprocess.run([bash, "-n", str(SETUP_SCRIPT)], check=True)


def test_setup_script_uses_the_generator_and_leaves_the_gateway_alone():
    text = SETUP_SCRIPT.read_text()
    assert "py stage" in text and "py apply" in text
    assert "cat > /etc/caddy/Caddyfile" not in text
    assert "UNIT=caddy-webspec.service" in text and "SOCKET=caddy-webspec.socket" in text
    assert "github.com/mholt/caddy-ratelimit" in text
    assert "XCADDY_SETCAP=0" in text
    # The gateway has its own installer: no edits to, or restarts of, its unit.
    assert "webspec-gateway.service" not in text
    assert "sed -i" not in text
    assert "SUDO_USER" not in text  # Caddy no longer runs as the login user


def test_setup_script_runs_python_isolated_from_the_working_directory():
    text = SETUP_SCRIPT.read_text()
    assert re.search(r"^cd /$", text, re.M)
    first_python = text.index('"$PYTHON" -I -c')
    assert text.index("\ncd /\n") < first_python
    # Every Python run goes through py(), and py() runs isolated (-I).
    assert len(re.findall(r'"\$PYTHON"', text)) == 1
    assert "python3 -c" not in text and "python -" not in text
    assert "PYTHONPATH" not in text


def test_setup_script_validates_before_it_changes_anything():
    text = SETUP_SCRIPT.read_text()
    stage = text.index("py stage")
    validate_staged = text.index('validate "${STAGE}/caddy" "${STAGE}/Caddyfile"')
    first_live_change = min(text.index('install -m 0755 -o root -g root "${WORK}/caddy" "${CADDY_BIN}.new"'),
                            text.index("py apply"),
                            text.index('mv -- "$LOG_DIR"'), text.index("install_if_changed \""))
    assert stage < validate_staged < first_live_change


def test_setup_script_pins_versions_and_verifies_go():
    text = SETUP_SCRIPT.read_text()
    assert 'CADDY_VERSION="${CADDY_VERSION:-v2.11.7}"' in text
    assert 'RATELIMIT_VERSION="${CADDY_RATELIMIT_VERSION:-v0.1.0}"' in text
    assert 'XCADDY_VERSION="${XCADDY_VERSION:-v0.4.7}"' in text
    assert '"${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}"' in text
    assert "xcaddy@${XCADDY_VERSION}" in text and "@latest" not in text
    sums = re.findall(r"1\.27\.1-(amd64|arm64)\) echo ([0-9a-f]{64})", text)
    assert sorted(arch for arch, _ in sums) == ["amd64", "arm64"]
    assert "sha256sum -c" in text
    assert "rm -rf /usr/local/go" not in text  # the build's Go stays in the work directory
    assert "GOENV=off" in text


def test_setup_script_moves_old_logs_and_blocks_aside():
    text = SETUP_SCRIPT.read_text()
    assert 'mv -- "$LOG_DIR" "$old_logs"' in text
    assert "chown -hR caddy:caddy" not in text  # old logs are no longer adopted in place
    assert '--disabled "${DISABLED_DIR}/${STAMP}"' in text
    assert caddy.CADDY_CONF_DIR.parent / "conf.d.disabled" == Path("/etc/caddy/conf.d.disabled")


def test_setup_script_never_falls_back_to_the_projects_domain():
    text = SETUP_SCRIPT.read_text()
    assert "i-a-m.live" not in text


# ── optional: the real Caddy ──
# WEBSPEC_TEST_CADDY=/path/to/caddy (built with github.com/mholt/caddy-ratelimit) runs
# `caddy validate`, `caddy fmt` and `caddy adapt` on the generated files.


def _run_caddy(binary: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run caddy and, when it refuses, fail with its own reason (CI shows nothing else)."""
    run = subprocess.run([binary, *args], capture_output=True, text=True, env=env)
    assert run.returncode == 0, f"caddy {' '.join(args)} exited {run.returncode}:\n{run.stderr}{run.stdout}"
    return run


@pytest.mark.skipif(not os.environ.get("WEBSPEC_TEST_CADDY"), reason="set WEBSPEC_TEST_CADDY to a caddy binary")
def test_real_caddy_validates_and_adapts_the_generated_config(tmp_path, monkeypatch):
    binary = os.environ["WEBSPEC_TEST_CADDY"]
    conf_dir = tmp_path / "conf.d"
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(generate_global_caddyfile(conf_dir=conf_dir, admin_socket=tmp_path / "admin.sock"))
    log = tmp_path / "log"
    for name, block in [("svc", generate_site_block("svc", "i-a-m.live", guard=True, log_dir=log)),
                        ("open", generate_site_block("open", "i-a-m.live", guard=False, log_dir=log)),
                        ("web", generate_direct_site_block("web", "i-a-m.live", target_port=3000, log_dir=log))]:
        write_site_block(name, block, conf_dir=conf_dir)

    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    _run_caddy(binary, "validate", "--config", str(caddyfile), env=env)
    for path in [caddyfile, *conf_dir.glob("*.caddy")]:
        fmt = subprocess.run([binary, "fmt", "--diff", str(path)], capture_output=True, text=True)
        assert fmt.returncode == 0, f"{path.name} is not caddy-fmt clean:\n{fmt.stdout}{fmt.stderr}"

    adapted = _run_caddy(binary, "adapt", "--config", str(caddyfile), env=env)
    cfg = json.loads(adapted.stdout)
    assert cfg["admin"]["listen"] == f"unix/{tmp_path}/admin.sock|0200"
    servers = list(cfg["apps"]["http"]["servers"].values())
    # C2: one server per listener, the gateway's services on fds 3 and 5, direct sites on 4 and 6.
    assert [s["listen"] for s in servers] == [["fd/3", "fd/5"], ["fd/4", "fd/6"]]
    hosts = [[tuple(h for m in r.get("match", []) for h in m.get("host", [])) for r in s["routes"]] for s in servers]
    assert hosts == [[("open.localhost",), ("svc.localhost", "svc.i-a-m.live"), ()],  # () is the catch-all, last
                     [("web.localhost", "web.i-a-m.live"), ()]]
    logs = cfg["logging"]["logs"]
    assert set(logs) == {"default", "log0", "log1", "log2"}
    for entry in logs.values():
        assert entry["encoder"]["format"] == "filter"
        assert entry["encoder"]["fields"] == {
            "request>uri": {"filter": "regexp", "regexp": caddy.URI_FILTER},
            "request>headers": {"filter": "delete"},
            "resp_headers": {"filter": "delete"},
        }
    handlers = [h for r in servers[0]["routes"] for h in _walk_handlers(r)]
    limits = [h["rate_limits"] for h in handlers if h.get("handler") == "rate_limit"]
    assert {name for lim in limits for name in lim} == {
        f"{svc}_{zone}" for svc in ("svc", "open") for zone in ("tunnel", "nonce_tunnel", "local", "nonce_local")}
    svc = next(lim for lim in limits if "svc_tunnel" in lim)
    nonce = {"path": ["/__nonce", "/__challenge"]}
    tunneled = {"header": {"Cf-Ray": ["*"]}}
    assert svc["svc_tunnel"]["key"] == svc["svc_nonce_tunnel"]["key"] == "{webspec_client}"
    assert svc["svc_tunnel"]["match"] == [{**tunneled, "not": [nonce]}]
    assert svc["svc_nonce_tunnel"]["match"] == [{**tunneled, **nonce}]
    assert svc["svc_local"]["key"] == svc["svc_nonce_local"]["key"] == "{http.request.remote.host}"
    assert svc["svc_local"]["match"] == [{"not": [tunneled, nonce]}]  # neither tunneled nor a nonce path
    assert svc["svc_nonce_local"]["match"] == [{"not": [tunneled], **nonce}]
    assert (svc["svc_tunnel"]["max_events"], svc["svc_nonce_tunnel"]["max_events"]) == (60, 120)
    maps = [h for h in handlers if h.get("handler") == "map"]
    assert len(maps) == 2 and maps[0]["source"] == "{http.request.header.Cf-Connecting-Ip}"
    assert maps[0]["destinations"] == ["{webspec_client}"]
    assert [(m["input_regexp"], m["outputs"]) for m in maps[0]["mappings"]] == \
        [(pattern, [key]) for pattern, key in caddy.client_key_rules()]


def _walk_handlers(node):
    if isinstance(node, dict):
        if "handler" in node:
            yield node
        for value in node.values():
            yield from _walk_handlers(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_handlers(value)
