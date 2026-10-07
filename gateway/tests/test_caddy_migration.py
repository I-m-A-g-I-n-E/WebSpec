"""webspec.caddy for setup-caddy.sh's migration, and the listeners every generated block binds.

- F26: the listeners come from the installed caddy-webspec.socket (as systemd reports it, else
  an earlier setup's IPv4-only drop-in), never from the kernel; a layout the unit does not pass
  is refused. P2: [::1] listeners that nothing holds, though the kernel has IPv6, are named.
- F8: direct sites have a listener of their own (contract C2), and cloudflared's rules send their
  public hosts there.
- F28: a tunneled IPv6 client is counted by its /64, however the address is written.
- F9: the hosts Caddy serves today, an earlier setup's blocks included, against those a plan
  serves; a plan that serves fewer, or may, is refused unless ALLOW_SHRINK=1.
- F30: the direct sites of an earlier setup are regenerated, never dropped.
- F31: the hint to restart the gateway after a domain change names this host's unit.
"""

from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import subprocess
from types import SimpleNamespace

import pytest

from webspec import caddy, config_writer
from webspec.caddy import (
    DUAL,
    IPV4_ONLY,
    MANAGED,
    REFUSED,
    CaddyError,
    DomainError,
    Site,
    apply_site_changes,
    caddy_status,
    caddyfile_sites,
    client_key_rules,
    gateway_domain_hint,
    generate_direct_site_block,
    generate_global_caddyfile,
    generate_site_block,
    host_report,
    ingress_rules,
    installed_listeners,
    legacy_direct_site,
    plan_sites,
    resolve_domain,
    served_today,
    write_site_block,
)
from tests.test_caddy_transaction import make_caddy_host

REAL_SYSTEMD_LISTEN = caddy._systemd_listen  # before any fixture stands in for it

# What main's gateway/tools/setup-caddy.sh (9b317d3) wrote, and the blocks main's webspec.caddy
# generated: Caddy bound :7001 itself, ran as the login user, and logged without filters.
MAIN_CADDYFILE = """{
    admin localhost:2019
    auto_https off
}

import /etc/caddy/conf.d/*.caddy

# Catch-all for unknown subdomains
:7001 {
    respond "Unknown service" 421
}
"""


def main_block(name: str, upstream_port: int, public: bool = True, domain: str = "i-a-m.live") -> str:
    hosts = f"http://{name}.localhost:7001" + (f", http://{name}.{domain}:7001" if public else "")
    return (f"{hosts} {{\n    reverse_proxy localhost:{upstream_port}\n    log {{\n"
            f"        output file /var/log/caddy/{name}.log {{\n            roll_size 10mb\n"
            "            roll_keep 5\n        }\n        format json\n    }\n}\n")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No live file of this machine is read: no Caddyfile, no /etc/webspec, no installed unit."""
    monkeypatch.setattr(caddy, "CADDYFILE", tmp_path / "no-etc-caddy" / "Caddyfile")
    monkeypatch.setattr(caddy, "CADDY_CONF_DIR", tmp_path / "no-etc-caddy" / "conf.d")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", tmp_path / "no-etc-webspec" / "config.json")
    monkeypatch.setattr(caddy, "GATEWAY_ENV", tmp_path / "no-etc-webspec" / "gateway.env")
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", tmp_path / "no-drop-in" / "10-ipv4-only.conf")
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: DUAL.addresses)
    monkeypatch.setattr(caddy, "_on_macos", lambda: False)
    monkeypatch.delenv("WEBSPEC_DOMAIN", raising=False)


# ── F26: the listeners of the installed socket unit ──


def _systemctl(monkeypatch, stdout: str, returncode: int = 0) -> list:
    """Make `systemctl show` print ``stdout``; return the calls it gets."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    real_which = caddy.shutil.which
    monkeypatch.setattr(caddy.shutil, "which",
                        lambda name, path=None: "/usr/bin/systemctl" if name == "systemctl" else real_which(name, path=path))
    monkeypatch.setattr(caddy.subprocess, "run", run)
    return calls


@pytest.fixture
def unit(monkeypatch):
    """The real _systemd_listen, behind a stand-in systemctl; the kernel must never be asked."""
    monkeypatch.setattr(caddy, "_systemd_listen", REAL_SYSTEMD_LISTEN)

    def no_kernel():
        raise AssertionError("webspec-ctl asked the kernel about IPv6 (F26)")

    monkeypatch.setattr(caddy, "ipv6_supported", no_kernel)
    return SimpleNamespace(show=lambda stdout, rc=0: _systemctl(monkeypatch, stdout, rc))


def _listen(*addresses: str, load: str = "loaded") -> str:
    return f"LoadState={load}\n" + "".join(f"Listen={a} (Stream)\n" for a in addresses)


def test_the_listeners_are_the_ones_systemd_reports_in_fd_order(unit, tmp_path, monkeypatch):
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", tmp_path / "10-ipv4-only.conf")
    calls = unit.show(_listen(*DUAL.addresses))
    assert installed_listeners() == DUAL
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/systemctl", "show", "--property=LoadState", "--property=Listen", "caddy-webspec.socket"]
    assert kwargs["env"]["PATH"] == caddy.SAFE_PATH and kwargs["cwd"] == "/"
    unit.show(_listen(*IPV4_ONLY.addresses))
    assert installed_listeners() == IPV4_ONLY


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1:7001", "[::1]:7001"),  # the unit of fc0c558, before direct sites had a listener
    ("[::1]:7001", "[::1]:7003", "127.0.0.1:7001", "127.0.0.1:7003"),  # another order: other fds
    ("127.0.0.1:7001",),
    (),
])
def test_a_unit_that_passes_other_sockets_is_refused(unit, addresses):
    unit.show(_listen(*addresses))
    with pytest.raises(CaddyError, match="setup-caddy.sh again"):
        installed_listeners()


@pytest.mark.parametrize("stdout,rc", [(_listen(load="not-found"), 0), ("", 1)])
def test_without_systemd_the_drop_in_decides(unit, tmp_path, monkeypatch, stdout, rc):
    # A container or chroot (systemctl cannot reach systemd), or the unit is not installed.
    dropin = tmp_path / "caddy-webspec.socket.d" / "10-ipv4-only.conf"
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", dropin)
    unit.show(stdout, rc)
    assert installed_listeners() == DUAL
    dropin.parent.mkdir()
    dropin.write_text("[Socket]\nListenStream=\n")
    assert installed_listeners() == IPV4_ONLY


def test_without_systemctl_the_drop_in_decides(unit, tmp_path, monkeypatch):
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", tmp_path / "10-ipv4-only.conf")
    monkeypatch.setattr(caddy.shutil, "which", lambda name, path=None: None)
    assert installed_listeners() == DUAL


def test_status_refuses_a_caddyfile_for_another_layout_than_the_unit_passes(tmp_path, monkeypatch):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")  # generated for "dual"
    before = host.snapshot()
    # The unit lost its IPv6 lines (a drop-in written by hand): the catch-alls bind fd/5 and fd/6,
    # which systemd no longer passes, so the next start would fail.
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: IPV4_ONLY.addresses)
    status, why = caddy_status()
    assert status == REFUSED and "'dual' layout" in why and "127.0.0.1:7001, 127.0.0.1:7003" in why
    with pytest.raises(CaddyError):
        apply_site_changes({"svc": generate_site_block("svc", "example.com", guard=True, listen=(3,))})
    assert host.snapshot() == before and host.caddy.calls() == []


# P2: IPv6 turned on or off at boot after setup. On a kernel booted with ipv6.disable=1 systemd
# ignores the unit's [::1] lines, and a Caddyfile written for both families cannot start; the
# other way round, [::1] is held but not served. webspec-ctl health reports either.
@pytest.mark.parametrize("recorded,passed,effect", [
    (DUAL, IPV4_ONLY, "Caddy cannot start, as it binds fd/5 and fd/6, the [::1] sockets, which systemd passes "
                      "only while the kernel has IPv6"),
    (IPV4_ONLY, DUAL, "Caddy does not serve [::1]:7001 and [::1]:7003, which systemd holds, so connections there "
                      "wait unanswered"),
])
def test_a_layout_mismatch_says_what_it_does_and_what_to_run(tmp_path, monkeypatch, recorded, passed, effect):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    host.caddyfile.write_text(generate_global_caddyfile(conf_dir=host.conf, domain="example.com", listen=recorded))
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: passed.addresses)
    mismatch = caddy.listener_mismatch()
    assert mismatch == caddy_status()[1]
    assert mismatch.startswith(f"{host.caddyfile} binds the sockets of the {recorded.name!r} layout, but "
                               f"caddy-webspec.socket passes {', '.join(passed.addresses)} ({passed.name!r}): ")
    assert effect in mismatch
    assert mismatch.endswith("Run gateway/tools/setup-caddy.sh again: it writes the configuration for the sockets "
                             "systemd passes on the kernel it runs on, so it must run again whenever IPv6 is turned "
                             "on or off at boot")
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: recorded.addresses)
    assert caddy.listener_mismatch() is None and caddy_status() == (MANAGED, "")


# P2, the other way round, on a host an earlier setup-caddy.sh set up without IPv6: its drop-in
# kept the socket unit's IPv4 lines alone, and booted with IPv6, nothing held [::1]:7001 and
# [::1]:7003. Any local user could listen there, and the Caddyfile and the unit still agreed, so
# nothing said so. The kernel is asked here only: what nothing binds, not what to bind (F26).
@pytest.mark.parametrize("dropin,cause,fix", [
    (True, "which an earlier gateway/tools/setup-caddy.sh wrote on a kernel without IPv6, drops its [::1] lines",
     "Run gateway/tools/setup-caddy.sh again: it removes that drop-in, and systemd then holds them"),
    (False, "its [::1] lines are dropped (systemctl cat caddy-webspec.socket shows where)",
     "Put them back, then run gateway/tools/setup-caddy.sh again"),
])
def test_ipv6_listeners_that_nothing_holds_are_named(tmp_path, monkeypatch, dropin, cause, fix):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    host.caddyfile.write_text(generate_global_caddyfile(conf_dir=host.conf, domain="example.com", listen=IPV4_ONLY))
    path = tmp_path / "caddy-webspec.socket.d" / "10-ipv4-only.conf"
    monkeypatch.setattr(caddy, "IPV4_ONLY_DROPIN", path)
    if dropin:
        path.parent.mkdir()
        path.write_text("[Socket]\nListenStream=\n")
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: IPV4_ONLY.addresses)
    unheld = caddy.unheld_listeners()
    assert caddy_status() == (MANAGED, "") and caddy.listener_mismatch() is None  # they agree
    assert unheld.startswith("caddy-webspec.socket holds 127.0.0.1:7001 and 127.0.0.1:7003 only, though this "
                             "kernel has IPv6: ")
    assert cause in unheld and (str(path) in unheld) == dropin
    assert ("Nothing holds [::1]:7001 and [::1]:7003, so any local process can listen there and receive what "
            f"local clients send to them (DP-9). {fix}") in unheld and unheld.endswith(fix)


@pytest.mark.parametrize("passed,ipv6", [
    (IPV4_ONLY.addresses, False),  # a kernel without IPv6: systemd ignores the [::1] lines
    (DUAL.addresses, True),  # held, whatever the configuration binds
    (DUAL.addresses, False),
    (None, True),  # systemd cannot say
])
def test_ipv6_listeners_are_not_called_unheld_where_systemd_holds_them_or_there_is_no_ipv6(
        tmp_path, monkeypatch, passed, ipv6):
    make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: passed)
    monkeypatch.setattr(caddy, "ipv6_supported", lambda: ipv6)
    assert caddy.unheld_listeners() is None


def test_no_mismatch_is_reported_where_nothing_can_be_compared(tmp_path, monkeypatch):
    host = make_caddy_host(tmp_path, monkeypatch, domain=None)  # no Caddyfile
    assert caddy.listener_mismatch() is None
    host.caddyfile.write_text("{\n\tadmin localhost:2019\n}\n")  # the pre-hardening layout
    assert caddy.listener_mismatch() is None
    host.caddyfile.write_text(generate_global_caddyfile(conf_dir=host.conf, listen=DUAL))
    host.caddyfile.chmod(0o644)  # root's, whatever the suite's umask
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: ("127.0.0.1:7001", "[::1]:7001"))  # fc0c558's unit
    assert "Run gateway/tools/setup-caddy.sh again" in caddy.listener_mismatch()


def test_status_refuses_a_caddyfile_from_before_the_direct_listener(tmp_path, monkeypatch):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    lines = host.caddyfile.read_text().split("\n")
    lines[1] = caddy.CADDYFILE_META_PREFIX + '{"domain":"example.com"}'  # as fc0c558's code wrote it
    host.caddyfile.write_text("\n".join(lines))
    status, why = caddy_status()
    assert status == REFUSED and "before direct sites had a listener of their own" in why


def test_status_accepts_the_layout_it_was_generated_for(tmp_path, monkeypatch):
    host = make_caddy_host(tmp_path, monkeypatch, domain="example.com")
    monkeypatch.setattr(caddy, "_systemd_listen", lambda: IPV4_ONLY.addresses)
    host.caddyfile.write_text(generate_global_caddyfile(conf_dir=host.conf, domain="example.com", listen=IPV4_ONLY))
    assert caddy_status() == (MANAGED, "")


# ── F8: direct sites are on a listener of their own (C2) ──


def test_the_layouts_follow_contract_c2():
    assert DUAL.addresses == ("127.0.0.1:7001", "127.0.0.1:7003", "[::1]:7001", "[::1]:7003")
    assert (DUAL.gateway, DUAL.direct) == ((3, 5), (4, 6))
    assert IPV4_ONLY.addresses == DUAL.addresses[:2] and (IPV4_ONLY.gateway, IPV4_ONLY.direct) == ((3,), (4,))
    assert DUAL.where("service") == "127.0.0.1:7001 and [::1]:7001"
    assert DUAL.where("direct") == "127.0.0.1:7003 and [::1]:7003"


def test_the_gateways_listener_serves_no_direct_site():
    # Whatever the agent may reach (127.0.0.1:7001, [::1]:7001) never proxies around the gateway.
    for layout in (DUAL, IPV4_ONLY):
        text = generate_global_caddyfile(listen=layout)
        direct = generate_direct_site_block("web", "example.com", target_port=3000, listen=layout.direct)
        binds = re.findall(r"^\tbind (.+)$", direct, re.M)
        assert binds == [" ".join(f"fd/{fd}" for fd in layout.direct)]
        assert not set(re.findall(r"fd/(\d)", direct)) & {str(fd) for fd in layout.gateway}
        assert f"\tbind {' '.join(f'fd/{fd}' for fd in layout.direct)}\n" in text.split("http://:7003 {")[1]


def test_ingress_rules_send_direct_sites_to_their_listener():
    sites = [Site("web", "direct", upstream="127.0.0.1:3000"), Site("svc", "service", guard=True),
             Site("app", "direct", upstream="[::1]:8080")]
    assert ingress_rules("example.com", sites) == [
        "ingress:",
        "  - hostname: app.example.com",
        "    service: http://127.0.0.1:7003",
        "  - hostname: web.example.com",
        "    service: http://127.0.0.1:7003",
        '  - hostname: "*.example.com"',
        "    service: http://127.0.0.1:7001",
        "  - service: http_status:404",
    ]
    assert ingress_rules("", sites) == []


def test_ingress_rules_default_to_the_trusted_blocks(tmp_path):
    write_site_block("web", generate_direct_site_block("web", "example.com", target_port=3000), conf_dir=tmp_path)
    (tmp_path / "evil.caddy").write_text("http://evil.example.com:7001 {\n}\n")  # not trusted: not listed
    rules = ingress_rules("example.com", conf_dir=tmp_path)
    assert "  - hostname: web.example.com" in rules and not any("evil" in line for line in rules)


# ── F28: a tunneled client is counted by its /64 ──


def _key(value: str) -> str:
    """The key Caddy's map gives ``value``: the first rule that matches, expanded as Go does."""
    for pattern, template in client_key_rules():
        m = re.search(pattern, value)
        if m:
            return m.expand(re.sub(r"\$\{(\d+)\}", r"\\g<\1>", template))
    raise AssertionError(f"no rule matches {value!r}")


def _spellings(groups: list[int]) -> set[str]:
    """Every way to write the address: in full, or with :: standing for any one run of zero groups."""
    text = [f"{g:x}" for g in groups]
    forms = {":".join(text)}
    for start in range(8):
        for end in range(start + 1, 9):
            if all(g == 0 for g in groups[start:end]):
                forms.add(":".join(text[:start]) + "::" + ":".join(text[end:]))
    return forms


def test_every_spelling_of_an_ipv6_address_gets_the_key_of_its_64():
    rng = random.Random(64)
    for _ in range(3000):
        # Zero groups are frequent, so that every place of :: is exercised.
        groups = [0 if rng.random() < 0.45 else rng.randrange(1, 0x10000) for _ in range(8)]
        expected = ":".join(f"{g:x}" for g in groups[:4]) + "::/64"
        for spelling in _spellings(groups):
            assert ipaddress.IPv6Address(spelling) == ipaddress.IPv6Address(":".join(f"{g:x}" for g in groups))
            assert _key(spelling) == expected, spelling


def test_the_64_key_separates_clients_and_leaves_other_values_alone():
    assert _key("2001:db8:1:2::1") == _key("2001:db8:1:2:dead:beef:0:1") == "2001:db8:1:2::/64"
    assert _key("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert _key("::1") == _key("::") == "0:0:0:0::/64"
    for value in ("203.0.113.7", "::ffff:203.0.113.7", "64:ff9b::203.0.113.7", "", "not an address"):
        assert _key(value) == value  # IPv4 clients, and anything else, count one by one


# ── F9: what Caddy serves today ──


def test_the_layout_from_before_setup_caddy_is_read_leniently():
    assert caddyfile_sites(MAIN_CADDYFILE, ["import /etc/caddy/conf.d/*.caddy"]) == ([], 0)
    assert caddyfile_sites(main_block("op-auth", 7002)) == (
        [("op-auth.localhost", 7001), ("op-auth.i-a-m.live", 7001)], 0)


@pytest.mark.parametrize("text,sites,unreadable", [
    ("(snippet) {\n\trespond 1\n}\n", [], 0),  # a snippet declares no site
    ("{\n\tadmin off\n}\n:7001 {\n}\n", [], 0),  # global options, and a catch-all with no host
    ("http://A.Example.com:80,\n  b.example.com {\n}\n", [("a.example.com", 80), ("b.example.com", None)], 0),
    ("*.w.example.com, [::1]:9 { # comment\n}\n", [("*.w.example.com", None), ("[::1]", 9)], 0),
    ("{$DOMAIN} {\n}\n", [], 1),  # a placeholder: no telling what it serves
    ("import /etc/caddy/sites/*\n", [], 1),  # an import of other files can declare any site
    ("(s) {\n\tx.example.com {\n\t}\n}\nimport s\n", [], 1),  # so can a snippet imported at the top
    ("x.example.com {\n\timport s\n}\n", [("x.example.com", None)], 0),  # inside a site it cannot
])
def test_sites_of_a_hand_made_caddyfile(text, sites, unreadable):
    assert caddyfile_sites(text) == (sites, unreadable)


@pytest.fixture
def old_host(tmp_path, monkeypatch):
    """main's layout: its Caddyfile, a guarded and an unguarded service, and a direct site."""
    etc = tmp_path / "etc-caddy"
    conf = etc / "conf.d"
    conf.mkdir(parents=True)
    (etc / "Caddyfile").write_text(MAIN_CADDYFILE.replace("/etc/caddy/conf.d", str(conf)))
    (conf / "op-auth.caddy").write_text(main_block("op-auth", 7002))
    (conf / "mail-proton.caddy").write_text(main_block("mail-proton", 7002, public=False))
    (conf / "app.caddy").write_text(main_block("app", 3000))
    monkeypatch.setattr(caddy, "CADDYFILE", etc / "Caddyfile")
    monkeypatch.setattr(caddy, "CADDY_CONF_DIR", conf)
    services = {"op-auth": SimpleNamespace(guard=True), "mail-proton": SimpleNamespace(guard=False)}
    return SimpleNamespace(etc=etc, conf=conf, caddyfile=etc / "Caddyfile", services=services)


def test_the_migration_keeps_every_host_and_moves_the_direct_site(old_host):
    plan = plan_sites(old_host.services, old_host.conf, adopt_legacy=True)
    assert plan.adopted == ["app"] and sorted(p.name for p in plan.untrusted) == [
        "app.caddy", "mail-proton.caddy", "op-auth.caddy"]
    report = host_report(plan, "i-a-m.live", conf_dir=old_host.conf, caddyfile=old_host.caddyfile)
    assert report.before == {"op-auth.localhost": {7001}, "op-auth.i-a-m.live": {7001},
                             "mail-proton.localhost": {7001}, "app.localhost": {7001}, "app.i-a-m.live": {7001}}
    assert report.dropped == [] and report.unreadable == []
    assert report.moved == ["app.i-a-m.live", "app.localhost"]  # to the direct listener (DP-3)
    assert report.after["app.i-a-m.live"] == 7003 and report.after["op-auth.i-a-m.live"] == 7001
    assert not report.refused(allow_shrink=False)


def test_a_direct_site_that_a_service_replaces_leaves_the_direct_listener(tmp_path, monkeypatch, capsys):
    # P10: setup-caddy.sh over a direct site whose name the gateway config now gives a service. Its
    # hosts go to the gateway's listener: "moved to the direct listener" would send cloudflared the
    # wrong way, to a 421.
    host = make_caddy_host(tmp_path, monkeypatch, domain="i-a-m.live")
    write_site_block("web", generate_direct_site_block("web", "i-a-m.live", target_port=8080, listen=DUAL.direct),
                     conf_dir=host.conf)
    plan = plan_sites({"web": SimpleNamespace(guard=True)}, host.conf, adopt_legacy=True)
    report = host_report(plan, "i-a-m.live", conf_dir=host.conf, caddyfile=host.caddyfile)
    assert report.moved == [] and report.left_direct == ["web.i-a-m.live", "web.localhost"]
    assert report.after == {"web.localhost": 7001, "web.i-a-m.live": 7001} and not report.refused(False)
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"web": {"command": "x", "guard": True}}}))
    hosts = tmp_path / "hosts"
    assert _main("plan", "--config", config, "--domain", "i-a-m.live", "--listeners", "dual",
                 "--hosts-out", hosts) == 0
    out = capsys.readouterr().out
    assert "Moved to the direct listener" not in out
    assert "No longer direct sites, now served through the gateway on 127.0.0.1:7001: web.i-a-m.live, " \
           "web.localhost." in out
    assert "cloudflared must stop sending their public hosts to :7003" in out
    assert hosts.read_text().splitlines() == ["web.i-a-m.live\t7001\tleft-direct\t7003",
                                              "web.localhost\t7001\tleft-direct\t7003"]


def test_a_plan_without_the_domain_drops_the_public_hosts(old_host):
    plan = plan_sites(old_host.services, old_host.conf, adopt_legacy=True)
    report = host_report(plan, "", conf_dir=old_host.conf, caddyfile=old_host.caddyfile)
    assert report.dropped == ["app.i-a-m.live", "op-auth.i-a-m.live"]
    assert report.refused(allow_shrink=False) and not report.refused(allow_shrink=True)


def test_a_plan_without_services_drops_theirs(old_host):
    # The default config of a production host, on a development host: no services in it.
    report = host_report(plan_sites({}, old_host.conf, adopt_legacy=True), "i-a-m.live",
                         conf_dir=old_host.conf, caddyfile=old_host.caddyfile)
    assert report.dropped == ["mail-proton.localhost", "op-auth.i-a-m.live", "op-auth.localhost"]


def test_files_whose_hosts_cannot_be_read_block_the_plan_too(old_host):
    (old_host.conf / "linked.caddy").symlink_to(old_host.conf / "op-auth.caddy")
    old_host.caddyfile.write_text(old_host.caddyfile.read_text() + "import /etc/caddy/more/*.caddy\n")
    hosts, unreadable = served_today(old_host.conf, old_host.caddyfile)
    assert len(unreadable) == 2
    assert any("linked.caddy' (not a regular UTF-8 file)" in entry for entry in unreadable)
    assert any("1 address(es) or import(s) that name no host" in entry for entry in unreadable)
    report = host_report(plan_sites(old_host.services, old_host.conf, adopt_legacy=True), "i-a-m.live",
                         conf_dir=old_host.conf, caddyfile=old_host.caddyfile)
    assert report.dropped == [] and report.refused(allow_shrink=False)


def test_the_generated_layout_reads_completely(tmp_path):
    conf = tmp_path / "conf.d"
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(generate_global_caddyfile(conf_dir=conf, domain="example.com", listen=DUAL))
    write_site_block("svc", generate_site_block("svc", "example.com", guard=True), conf_dir=conf)
    write_site_block("web", generate_direct_site_block("web", "example.com", target_port=3000), conf_dir=conf)
    hosts, unreadable = served_today(conf, caddyfile)
    assert unreadable == []
    assert hosts == {"svc.localhost": {7001}, "svc.example.com": {7001},
                     "web.localhost": {7003}, "web.example.com": {7003}}


# ── F30: the direct sites of an earlier setup ──


@pytest.mark.parametrize("line,upstream", [
    ("    reverse_proxy localhost:3000", "127.0.0.1:3000"),  # localhost means 127.0.0.1, as for add --port
    ("\treverse_proxy 127.0.0.1:20128", "127.0.0.1:20128"),
    ("    reverse_proxy http://[::1]:8080", "[::1]:8080"),
])
def test_an_old_direct_block_is_regenerated_from_its_port(tmp_path, line, upstream):
    path = tmp_path / "app.caddy"
    path.write_text(main_block("app", 1).replace("    reverse_proxy localhost:1", line))
    assert legacy_direct_site(path) == Site("app", "direct", upstream=upstream)


@pytest.mark.parametrize("name,text", [
    ("svc.caddy", main_block("svc", 7002)),  # a service block: the gateway config decides on it
    ("loop.caddy", main_block("loop", 7001)),  # through Caddy itself
    ("loop.caddy", main_block("loop", 7003)),
    ("two.caddy", main_block("two", 3000) + "    reverse_proxy localhost:3001\n"),  # which one?
    ("far.caddy", main_block("far", 3000).replace("localhost:3000", "example.com:80")),  # not loopback
    ("Bad_Name.caddy", main_block("bad", 3000)),  # its name is no DNS label
    ("none.caddy", "http://none.localhost:7001 {\n\trespond 200\n}\n"),
])
def test_other_blocks_are_not_taken_for_direct_sites(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    assert legacy_direct_site(path) is None


def test_a_linked_or_special_block_is_not_followed(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text(main_block("app", 3000))
    (tmp_path / "app.caddy").symlink_to(target)
    assert legacy_direct_site(tmp_path / "app.caddy") is None
    os.mkfifo(tmp_path / "fifo.caddy")
    assert legacy_direct_site(tmp_path / "fifo.caddy") is None  # and does not hang


def test_the_domain_of_the_old_blocks_carries_over(old_host):
    # sudo drops WEBSPEC_DOMAIN: the first run of setup-caddy.sh keeps the domain the old blocks
    # serve, rather than dropping every public host (F9).
    domain = resolve_domain({})
    assert (domain.name, domain.explicit) == ("i-a-m.live", False)
    assert "previous setup" in domain.source
    (old_host.conf / "other.caddy").write_text(main_block("other", 3001, domain="other.dev"))
    with pytest.raises(DomainError, match="more than one public domain"):
        resolve_domain({})
    assert resolve_domain({"WEBSPEC_DOMAIN": "i-a-m.live"}).name == "i-a-m.live"


# ── the command line setup-caddy.sh runs: plan, stage, apply ──


def _main(*argv) -> int:
    return caddy.main([str(a) for a in argv])


@pytest.fixture
def old_cli(old_host, tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"op-auth": {"command": "x", "guard": True},
                                                 "mail-proton": {"command": "x"}}}))
    old_host.config = config
    old_host.state = lambda: {p.name: p.read_bytes() for p in (*old_host.conf.iterdir(), old_host.caddyfile)}
    return old_host


def test_cli_plan_names_the_hosts_it_would_drop_and_changes_nothing(old_cli, tmp_path, capsys):
    before = old_cli.state()
    hosts = tmp_path / "hosts"
    assert _main("plan", "--config", old_cli.config, "--domain", "", "--listeners", "dual",
                 "--hosts-out", hosts) == caddy.SHRINK_REFUSED
    captured = capsys.readouterr()
    assert "these hosts are served today, and would not be served afterwards:\n  app.i-a-m.live\n" \
           "  op-auth.i-a-m.live\n" in captured.err
    assert "Hosts the configuration on disk serves: 5; still served afterwards: 3." in captured.out
    assert old_cli.state() == before
    # What setup-caddy.sh probes before and after: host, port afterwards, state, port today.
    assert hosts.read_text().splitlines() == [
        "app.i-a-m.live\t7001\tdropped\t7001", "app.localhost\t7003\tmoved\t7001",
        "mail-proton.localhost\t7001\tkept\t7001", "op-auth.i-a-m.live\t7001\tdropped\t7001",
        "op-auth.localhost\t7001\tkept\t7001"]
    assert _main("plan", "--config", old_cli.config, "--domain", "", "--listeners", "dual", "--allow-shrink") == 0
    assert "No longer served, as ALLOW_SHRINK=1 allows: app.i-a-m.live, op-auth.i-a-m.live." in capsys.readouterr().out


@pytest.mark.parametrize("command", ["stage", "apply"])
def test_cli_stage_and_apply_refuse_to_shrink_on_their_own(old_cli, tmp_path, command, capsys):
    before = old_cli.state()
    extra = ["--out", tmp_path / "stage"] if command == "stage" else ["--disabled", tmp_path / "disabled" / "ts"]
    assert _main(command, "--config", old_cli.config, "--domain", "", "--listeners", "dual",
                 *extra) == caddy.SHRINK_REFUSED
    assert "may stop serving app.i-a-m.live, op-auth.i-a-m.live" in capsys.readouterr().err
    assert old_cli.state() == before and not (tmp_path / "stage").exists() and not (tmp_path / "disabled").exists()


def test_cli_migrates_the_old_layout_with_every_host(old_cli, tmp_path, capsys):
    hosts = tmp_path / "hosts"
    assert _main("plan", "--config", old_cli.config, "--domain", "i-a-m.live", "--listeners", "dual",
                 "--hosts-out", hosts) == 0
    out = capsys.readouterr().out
    assert "Moved to the direct listener, 127.0.0.1:7003 (DP-3): app.i-a-m.live, app.localhost." in out
    assert "app.caddy                direct site -> 127.0.0.1:3000: app.localhost and app.i-a-m.live, on the " \
           "direct listener (:7003). It bypasses the gateway: keep :7003 out of the agent's allow-list (DP-3)" in out
    assert "app.localhost\t7003\tmoved\t7001" in hosts.read_text().splitlines()
    assert _main("apply", "--config", old_cli.config, "--domain", "i-a-m.live", "--listeners", "dual",
                 "--disabled", tmp_path / "disabled" / "ts") == 0
    assert sorted(p.name for p in old_cli.conf.iterdir()) == ["app.caddy", "mail-proton.caddy", "op-auth.caddy"]
    assert caddy.read_site(old_cli.conf / "app.caddy") == Site("app", "direct", upstream="127.0.0.1:3000")
    after, unreadable = served_today(old_cli.conf, old_cli.caddyfile)
    assert unreadable == [] and set(after) == {"op-auth.localhost", "op-auth.i-a-m.live", "mail-proton.localhost",
                                                "app.localhost", "app.i-a-m.live"}
    assert after["app.i-a-m.live"] == {7003}
    assert "'app.caddy': regenerated as a direct site, on the direct listener (:7003)" in capsys.readouterr().out


def test_cli_ingress_prints_cloudflareds_rules(old_cli, capsys):
    write_site_block("web", generate_direct_site_block("web", "i-a-m.live", target_port=3000), conf_dir=old_cli.conf)
    assert _main("ingress", "--domain", "i-a-m.live") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == ["ingress:", "  - hostname: web.i-a-m.live", "    service: http://127.0.0.1:7003"]
    assert _main("ingress", "--domain", "") == 0 and capsys.readouterr().out == "\n"


# ── F31: the gateway reads WEBSPEC_DOMAIN only when it starts ──


def test_the_restart_hint_names_the_production_unit(tmp_path, monkeypatch):
    production = tmp_path / "etc-webspec" / "config.json"
    production.parent.mkdir()
    production.write_text("{}")
    monkeypatch.setattr(config_writer, "PRODUCTION_CONFIG", production)
    monkeypatch.setattr(caddy, "_system_unit_loaded", lambda unit: pytest.fail("not asked on production"))
    hint = gateway_domain_hint("example.com")
    assert f"Set WEBSPEC_DOMAIN=example.com in {caddy.GATEWAY_ENV}" in hint
    assert "(sudo systemctl restart webspec-gateway)" in hint and "404" in hint


def test_the_restart_hint_names_a_system_unit_of_an_earlier_setup(monkeypatch):
    asked = []
    monkeypatch.setattr(caddy, "_system_unit_loaded", lambda unit: asked.append(unit) or True)
    hint = gateway_domain_hint("example.com")
    assert asked == ["webspec-gateway.service"]
    assert "environment of webspec-gateway.service" in hint and "(sudo systemctl restart webspec-gateway)" in hint


def test_the_restart_hint_names_the_development_unit(monkeypatch):
    monkeypatch.setattr(caddy, "_system_unit_loaded", lambda unit: False)
    hint = gateway_domain_hint("example.com")
    assert "~/.webspec/gateway.env" in hint and "systemctl --user restart webspec-gateway" in hint
    assert "sudo systemctl" not in hint and "/etc/webspec" not in hint  # F36: no /etc/webspec on a dev host


def test_a_loaded_system_unit_is_what_systemctl_says(monkeypatch):
    calls = _systemctl(monkeypatch, "loaded\n")
    assert caddy._system_unit_loaded("webspec-gateway.service") is True
    assert calls[0][0] == ["/usr/bin/systemctl", "show", "--property=LoadState", "--value", "webspec-gateway.service"]
    _systemctl(monkeypatch, "not-found\n")
    assert caddy._system_unit_loaded("webspec-gateway.service") is False
    _systemctl(monkeypatch, "", 1)
    assert caddy._system_unit_loaded("webspec-gateway.service") is False
