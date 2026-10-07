"""Caddy configuration for the WebSpec gateway: global Caddyfile, per-service site blocks, reload.

Deployment (docs/spec/audit-deployment.md)::

    cloudflared ──▶ 127.0.0.1:7001, [::1]:7001 ──▶ Caddy ──▶ gateway 127.0.0.1:7002
                ──▶ 127.0.0.1:7003, [::1]:7003 ──▶ Caddy ──▶ direct sites (local web apps)
                    held by systemd (caddy-webspec.socket)

- **DP-9** Caddy opens no listening socket of its own. systemd binds the four sockets
  (``gateway/deploy/linux/caddy-webspec.socket``) and passes them to Caddy as file descriptors
  3 to 6 (:class:`Listeners`). The generated configuration binds only those, with an explicit
  ``bind`` in every site block. systemd keeps the sockets bound while Caddy restarts or
  crashes, so no other local process can take a port in between and receive cloudflared's
  traffic.
- **DP-5** Caddy forwards only hosts that have a site block: ``{name}.localhost`` for every
  service, and ``{name}.{domain}`` only for guarded ones. Every other Host gets a catch-all's
  421. ``reverse_proxy`` passes the Host header through unchanged, as the guard signs it. Both
  cloudflared and local processes connect over loopback, so the source address cannot tell
  them apart. A request that carries Cloudflare's ``Cf-Ray`` header came through the tunnel,
  and if it names a ``*.localhost`` host it is refused, because the gateway would treat it as
  local.
- **DP-3** A direct site (``webspec-ctl add --direct``) proxies straight to a local web app,
  around the gateway, its guard and its audit log. Direct sites have a listener of their own,
  :7003, so that the agent's egress allow-list can name the gateway's listener alone
  (127.0.0.1:7001 and [::1]:7001): on :7001 a direct site would hand the agent whatever the app
  can reach. cloudflared sends each direct site's public host to 127.0.0.1:7003 with an
  ingress rule of its own (:func:`ingress_rules`).
- **DP-6** Logs never contain query strings or headers. The URI loses its query, where GET
  arguments travel, and the scheme, authority and userinfo of an absolute-form or CONNECT
  target, where a local client can put a password. Request and response headers are dropped
  entirely: they carry guard tags, nonces, clearances, approvals and cookies, and a direct
  site may use any header for credentials. The default logger gets the same filter, because
  Caddy's error entries (for example, a 502 while the gateway is down) include the request.
- The admin API can rewrite the whole configuration, so it does not listen on TCP
  ``localhost:2019``, where any local process could reach it. It listens on a unix socket
  (mode 0200) in the ``caddy`` user's 0700 RuntimeDirectory, so only root and ``caddy`` can
  reload. See ``gateway/deploy/linux/caddy-webspec.service``.
- Rate limits count every request to a service, /__nonce and /__challenge included (in zones
  of their own). A request that carries ``Cf-Ray`` counts in the tunnel zones, per client:
  the /64 of an IPv6 address, or the IPv4 address, in ``Cf-Connecting-Ip``. Any other request
  counts in the local zones, per source address. Exactly what that guarantees: each client
  that comes through the tunnel, whose headers Cloudflare sets, has an allowance of its own,
  which the other tunneled clients cannot spend. It binds no local process, the agent
  included: on loopback Caddy cannot tell one from cloudflared, so a process that sends both
  headers itself, to a guarded service's public host, picks its zone and its key (a
  ``*.localhost`` host refuses ``Cf-Ray`` with 421). It escapes the local count (a new
  ``Cf-Connecting-Ip`` for each request), or spends the allowance of a public client whose
  address it knows. One that sends neither is counted per source address, and on Linux it
  can use any address of 127.0.0.0/8 unless its egress rules pin the source. The guard checks
  every request to a guarded service all the same: the zones decide how requests are
  counted, not what they may do. A limit on the public clients as a whole is Cloudflare's to
  enforce, at the edge. That is a known limit; a listener that only cloudflared can reach
  would let Caddy tell the tunnel apart.
- Upstreams are dialed by IP, never as ``localhost``: a name may resolve to ::1 first, where
  any local user could listen on the same port and take the traffic.

Site blocks live in ``/etc/caddy/conf.d``. Each starts with :data:`GENERATED_MARKER` and a
metadata line (``# webspec-site: {...}``) from which it can be regenerated. Only a block that
is a regular file, owned by root (or by the user running this code), writable by its owner
only, and carries both lines for its own name is trusted: :func:`sync_caddy_config` rewrites
and removes trusted blocks only. ``gateway/tools/setup-caddy.sh`` moves every other ``*.caddy``
file out of the import glob, and writes the configuration through :func:`main`, the command
line of this module.

``webspec-ctl`` changes the live configuration through :func:`apply_site_changes` only, and only
on a host that setup-caddy.sh has migrated (:func:`caddy_status`): there the Caddyfile is the
generated one, whose servers bind systemd's sockets like the blocks do, namely the sockets the
installed socket unit passes (:func:`installed_listeners`). Each change is staged and checked
with ``caddy validate``, then written and loaded with ``caddy reload``. If Caddy does not load
it, every file is put back, so a rejected change never leaves a configuration that cannot
start. The caddy it runs is never looked up on PATH: it is /usr/local/bin/caddy or
``WEBSPEC_CADDY_BIN``, as root only if no other user can replace it, and it runs as the
``caddy`` user, as the service does. The Caddyfile's second line records the public domain
and the listener layout; ``webspec-ctl`` keeps serving that domain, or none, unless told
otherwise (``WEBSPEC_DOMAIN``, or ``caddy-sync --no-public``): sudo drops ``WEBSPEC_DOMAIN``, and
a routine command must neither take the public addresses down nor put them back.

setup-caddy.sh never shrinks the set of hosts Caddy serves without ``ALLOW_SHRINK=1``
(:func:`host_report`): it counts the blocks an earlier setup left, and regenerates the direct
sites among them (:func:`legacy_direct_site`) instead of dropping them. A file whose hosts it
cannot read (a symlink, a placeholder, an import of other files) counts as one it may drop.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import grp
import ipaddress
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Sequence

from . import config_writer
from .config_writer import PRODUCTION_ENV, is_production_host
from .hostgrammar import is_label

if TYPE_CHECKING:
    from .config import ServiceEntry, ServiceRegistry

logger = logging.getLogger("webspec.caddy")

CADDY_BIN = Path("/usr/local/bin/caddy")  # installed by gateway/tools/setup-caddy.sh
# Another caddy for webspec-ctl to run, by absolute path. A bare `caddy` is never looked up on
# PATH: macOS sudo keeps the caller's PATH, so as root that would run whatever the agent put in
# a directory it can write (DP-1, DP-4).
CADDY_BIN_ENV = "WEBSPEC_CADDY_BIN"
# caddy-webspec.service's user and state directory. As root, webspec-ctl runs caddy as this user
# with the unit's XDG directories, the way setup-caddy.sh validates.
CADDY_USER = "caddy"
CADDY_STATE_DIR = Path("/var/lib/caddy-webspec")
# The PATH caddy runs with: none of the caller's environment reaches it.
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CADDY_TIMEOUT = 30  # seconds for one `caddy validate` or `caddy reload`
CADDYFILE = Path("/etc/caddy/Caddyfile")
CADDY_CONF_DIR = Path("/etc/caddy/conf.d")
CADDY_LOG_DIR = Path("/var/log/caddy")
# RuntimeDirectory=caddy-webspec in gateway/deploy/linux/caddy-webspec.service (mode 0700).
CADDY_ADMIN_SOCKET = Path("/run/caddy-webspec/admin.sock")
# Only the owner may connect (connect(2) on a unix socket needs write permission). This is
# also Caddy's own default, and is spelled out here so that the intent is explicit.
CADDY_ADMIN_SOCKET_MODE = "0200"
DEFAULT_RATE_LIMIT = 60  # requests per minute per client
DEFAULT_RATE_WINDOW = "1m"
# /__nonce and /__challenge: each tool call needs a nonce, so their zones allow twice as much.
NONCE_PATHS = ("/__nonce", "/__challenge")

# Caddy's two listeners (contract C2). The gateway's services are on CADDY_PORT, the one port
# of Caddy's that the agent's egress allow-list names (127.0.0.1:7001 and [::1]:7001, DP-3).
# Direct sites, which bypass the gateway, are on DIRECT_PORT, which it never names.
CADDY_PORT = 7001
DIRECT_PORT = 7003
SOCKET_UNIT = "caddy-webspec.socket"
# An earlier setup-caddy.sh wrote this drop-in on a kernel without IPv6: it kept only the unit's
# IPv4 lines, for good. setup-caddy.sh now removes it: systemd ignores the [::1] lines by itself
# on such a kernel, and holds them on any other (unheld_listeners).
IPV4_ONLY_DROPIN = Path("/etc/systemd/system/caddy-webspec.socket.d/10-ipv4-only.conf")
SYSTEMCTL_TIMEOUT = 10  # seconds for `systemctl show`

# A production host (gateway/deploy/) keeps the gateway's WEBSPEC_DOMAIN here.
GATEWAY_ENV = PRODUCTION_ENV

# First line of every file this module generates.
GENERATED_MARKER = "# Generated by webspec.caddy"
# Second line of every site block: what it serves, as JSON, so that it can be regenerated.
SITE_META_PREFIX = "# webspec-site: "
# Second line of the generated Caddyfile: the public domain the configuration serves, as JSON
# ({"domain": ""} for loopback names only). webspec-ctl keeps it when WEBSPEC_DOMAIN is unset.
CADDYFILE_META_PREFIX = "# webspec-caddy: "

# DP-5: Cloudflare adds this header to the requests it proxies to the origin. When it appears
# with a *.localhost Host, the request may have come in through the tunnel with its Host
# rewritten (by cloudflared's httpHostHeader, for example), and it is refused. A local client
# has no need to send it, but can: with a public Host, its request then counts as tunneled
# (the rate limits in the module docstring).
TUNNEL_HEADER = "Cf-Ray"
# Set by Cloudflare to the address of the client it is proxying for; a local client can send
# any value.
TUNNEL_CLIENT_HEADER = "Cf-Connecting-Ip"
# The placeholder that a service block's `map` sets from TUNNEL_CLIENT_HEADER: the key a
# tunneled client is counted by (client_key_rules).
CLIENT_KEY = "webspec_client"

# DP-6: the field filters for every access log and for the default logger.
# Field paths follow Caddy's JSON log entry. Header keys appear in Go's canonical form
# (X-Webspec-Guard, not X-WebSpec-Guard), so a deny-list written with the spec's spelling
# would silently match nothing. Dropping the whole header objects avoids that problem.
# The URI keeps its path only: the query goes (GET arguments), and so do the scheme, authority
# and userinfo of an absolute-form target (`GET http://user:pass@host/...`) and the userinfo of
# a CONNECT one (`user:pass@host:port`). Only a local client sends those, but a password must
# not reach the logs either way (Caddy logs the target as it was sent).
URI_FILTER = "^[A-Za-z][A-Za-z0-9+.-]*://[^/?]*|^[^/?]*@|[?].*"
LOG_FILTER_FIELDS: tuple[str, ...] = (
    f'request>uri regexp {URI_FILTER} ""',  # keep the path only
    "request>headers delete",  # X-WebSpec-*, X-UFO-*, X-Gimme-Definer, Authorization, Cookie, ...
    "resp_headers delete",  # Set-Cookie, and Location URLs that may carry a query
)

_DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h|d))+")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
_SUFFIX = ".caddy"


# ── validation: generated text must stay well-formed Caddyfile ──


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not is_label(name):
        raise ValueError(f"service name {name!r} is not a lowercase DNS label")
    return name


def _check_domain(domain: str) -> str:
    if not all(is_label(part) for part in domain.split(".")):
        raise ValueError(f"domain {domain!r} is not a lowercase DNS name")
    return domain


def _check_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"port {port!r} is not in 1-65535")
    return port


def _check_path(path: Path) -> str:
    text = str(path)
    if not _SAFE_PATH.fullmatch(text):
        raise ValueError(f"path {text!r} must be absolute and use only [A-Za-z0-9._/-]")
    return text


def _loopback(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse a loopback IP literal, with or without brackets. Names are refused."""
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        raise ValueError(f"{host!r} is not an IP address; dial loopback by IP (127.0.0.1 or ::1)") from None
    if not ip.is_loopback:
        raise ValueError(f"{host!r} is not a loopback address; Caddy reaches loopback only (DP-5)")
    return ip


def upstream_address(host: str, port: int) -> str:
    """Return ``host:port`` for a loopback upstream, IPv6 in brackets. Refuses anything else."""
    ip = _loopback(host)
    _check_port(port)
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def _parse_upstream(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"upstream {text!r} is not host:port")
    address = upstream_address(host, int(port))
    if address != text:
        raise ValueError(f"upstream {text!r} is not in canonical form ({address})")
    return host.strip("[]"), int(port)


def reserved_port_owner(port: int, gateway_port: int = 7002) -> str | None:
    """Whose ``port`` is, if a direct site must never proxy to it: Caddy's listeners and the gateway's.

    Through Caddy's own listener every request would go round through Caddy, a connection
    more each time, until a header limit stopped it (431). Through the gateway, a direct site
    would serve the gateway's services around their site blocks (and its 404 lists them all,
    local ones included). Whatever the loopback address: Caddy holds both 127.0.0.1 and [::1].
    """
    if port == CADDY_PORT:
        return f"Caddy's gateway listener (:{CADDY_PORT})"
    if port == DIRECT_PORT:
        return f"Caddy's direct listener (:{DIRECT_PORT})"
    if port == gateway_port:
        return f"the gateway's port (:{gateway_port})"
    return None


def check_direct_upstream(host: str, port: int, gateway_port: int = 7002) -> str:
    """The address a new direct site proxies to: an app on loopback, never Caddy or the gateway.

    Raises ValueError for anything but a loopback IP (:func:`upstream_address`), and for a
    port of :func:`reserved_port_owner`'s.
    """
    address = upstream_address(host, port)
    owner = reserved_port_owner(port, gateway_port)
    if owner:
        hint = ("to serve a gateway service, add it without --direct" if port == gateway_port
                else "each request would loop through Caddy")
        raise ValueError(f"{address} is {owner}, not an app: a direct site must point at the app itself, "
                         f"at the port it listens on ({hint})")
    return address


# ── listening sockets (DP-3, DP-9) ──


@dataclass(frozen=True)
class Listeners:
    """The sockets caddy-webspec.socket passes to Caddy, by what they serve (contract C2).

    systemd passes them from fd 3 up, in the order of the unit's ListenStream lines:
    127.0.0.1:7001, 127.0.0.1:7003, [::1]:7001, [::1]:7003. On a kernel without IPv6, systemd
    ignores the last two, so the IPv4 sockets are fds 3 and 4 in both layouts.
    """

    name: str  # "dual" or "ipv4": what setup-caddy.sh and the Caddyfile's record call it
    gateway: tuple[int, ...]  # :7001, the gateway's services and their catch-all
    direct: tuple[int, ...]  # :7003, direct sites and their catch-all
    addresses: tuple[str, ...]  # what `systemctl show -p Listen` reports, in fd order

    def where(self, kind: str) -> str:
        """The addresses of the gateway's listener (kind "service") or the direct one ("direct")."""
        port = DIRECT_PORT if kind == "direct" else CADDY_PORT
        return " and ".join(a for a in self.addresses if a.endswith(f":{port}"))


DUAL = Listeners("dual", (3, 5), (4, 6), (f"127.0.0.1:{CADDY_PORT}", f"127.0.0.1:{DIRECT_PORT}",
                                          f"[::1]:{CADDY_PORT}", f"[::1]:{DIRECT_PORT}"))
IPV4_ONLY = Listeners("ipv4", (3,), (4,), (f"127.0.0.1:{CADDY_PORT}", f"127.0.0.1:{DIRECT_PORT}"))
LAYOUTS: dict[str, Listeners] = {layout.name: layout for layout in (DUAL, IPV4_ONLY)}


def ipv6_supported() -> bool:
    """True if this kernel has IPv6, so that caddy-webspec.socket can bind [::1].

    The socket unit sets FreeBind=yes, which binds [::1] even while the loopback interface
    has no IPv6 address. Only a kernel without IPv6 (booted with ipv6.disable=1) cannot, and
    there systemd ignores the unit's [::1] lines.
    """
    if not socket.has_ipv6:
        return False
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
    except OSError:
        return False
    return True


def planned_listeners() -> Listeners:
    """The layout setup-caddy.sh generates for: both families, or IPv4 only on a kernel without IPv6.

    The sockets that systemd will pass on this kernel. Only setup-caddy.sh asks the kernel, when
    it writes the configuration; everything after that binds what the installed unit passes
    (:func:`installed_listeners`).
    """
    return DUAL if ipv6_supported() else IPV4_ONLY


def _systemd_listen() -> tuple[str, ...] | None:
    """The listeners systemd reports for caddy-webspec.socket, in fd order; None if it cannot say.

    None when there is no systemctl or systemd (a container, macOS), or the unit is not loaded.
    """
    systemctl = shutil.which("systemctl", path=SAFE_PATH)
    if systemctl is None:
        return None
    try:
        result = subprocess.run([systemctl, "show", "--property=LoadState", "--property=Listen", SOCKET_UNIT],
                                capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT,
                                env={"PATH": SAFE_PATH, "LC_ALL": "C"}, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    load, listen = None, []
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "LoadState":
            load = value
        elif key == "Listen":
            m = re.fullmatch(r"(\S+) \(Stream\)", value)  # "127.0.0.1:7001 (Stream)"
            listen.append(m.group(1) if m else value)
    return tuple(listen) if load == "loaded" else None


def installed_listeners() -> Listeners:
    """The listeners of the installed caddy-webspec.socket: the sockets Caddy actually receives.

    systemd says which, in fd order (``systemctl show -p Listen``). Where it cannot be asked
    (no systemd, or the unit is not installed), the IPv4-only drop-in of an earlier
    setup-caddy.sh decides, if it is there. Never the kernel: whether IPv6 works now says
    nothing about what the unit passes, and a configuration that binds a socket systemd does
    not pass cannot start (F26). Raises CaddyError if the unit passes anything but a layout of
    :data:`LAYOUTS`.
    """
    reported = _systemd_listen()
    if reported is None:
        return IPV4_ONLY if IPV4_ONLY_DROPIN.exists() else DUAL
    for layout in LAYOUTS.values():
        if reported == layout.addresses:
            return layout
    raise CaddyError(
        f"{SOCKET_UNIT} passes {', '.join(reported) or 'no sockets'}, not the listeners this webspec.caddy "
        f"generates for ({' '.join(DUAL.addresses)}, or the first two without IPv6). Run "
        "gateway/tools/setup-caddy.sh again: it installs the unit and the configuration together")


def _check_listeners(listen: Any) -> Listeners:
    if not isinstance(listen, Listeners) or LAYOUTS.get(listen.name) != listen:
        raise ValueError(f"{listen!r} is not a listener layout of caddy-webspec.socket ({', '.join(LAYOUTS)})")
    return listen


def _bind_args(fds: Sequence[int], allowed: Sequence[int], listener: str) -> str:
    """Return the ``bind`` arguments for systemd's sockets. Caddy binds nothing else (DP-9)."""
    chosen = tuple(fds)
    if not chosen:
        raise ValueError(f"at least one file descriptor of the {listener} is required")
    for fd in chosen:
        if isinstance(fd, bool) or fd not in allowed:
            raise ValueError(f"file descriptor {fd!r} is not one of the {listener} that caddy-webspec.socket "
                             f"passes ({allowed})")
    if len(set(chosen)) != len(chosen):
        raise ValueError(f"duplicate file descriptors in {chosen}")
    return " ".join(f"fd/{fd}" for fd in chosen)


def _gateway_binds(listen: Sequence[int] | None) -> str:
    """``bind`` for the gateway's listener (:7001); ``listen`` None means the installed unit's."""
    fds = installed_listeners().gateway if listen is None else listen
    return _bind_args(fds, DUAL.gateway, f"gateway's listener (:{CADDY_PORT})")


def _direct_binds(listen: Sequence[int] | None) -> str:
    """``bind`` for the direct listener (:7003); ``listen`` None means the installed unit's."""
    fds = installed_listeners().direct if listen is None else listen
    return _bind_args(fds, DUAL.direct, f"direct listener (:{DIRECT_PORT})")


# ── shared fragments ──


def _site_hosts(name: str, domain: str | None, public: bool) -> list[str]:
    hosts = [f"{name}.localhost"]
    if public and domain:
        hosts.append(f"{name}.{_check_domain(domain)}")
    return hosts


def _site_addresses(name: str, domain: str | None, caddy_port: int, public: bool) -> str:
    return ", ".join(f"http://{host}:{caddy_port}" for host in _site_hosts(name, domain, public))


def _filter_lines(depth: int) -> list[str]:
    tab = "\t" * depth
    return [
        f"{tab}format filter {{",
        f"{tab}\twrap json",
        f"{tab}\tfields {{",
        *(f"{tab}\t\t{field}" for field in LOG_FILTER_FIELDS),
        f"{tab}\t}}",
        f"{tab}}}",
    ]


def _common_site_lines(name: str, binds: str, listener: str) -> list[str]:
    """Return the lines every site block starts with: the bind and the tunnel check."""
    return [
        f"\t# DP-9: only systemd's loopback sockets (caddy-webspec.socket), {listener}.",
        f"\tbind {binds}",
        "",
        "\t# DP-5: a *.localhost host never comes through the public tunnel. The gateway would",
        "\t# treat such a request as local and serve level-0 destinations to it.",
        "\t@tunneled_loopback {",
        f"\t\thost {name}.localhost",
        f"\t\theader {TUNNEL_HEADER} *",
        "\t}",
        '\trespond @tunneled_loopback "Loopback hosts are not served through the tunnel" 421 {',
        "\t\tclose",
        "\t}",
    ]


def _log_lines(name: str, log_dir: Path | None) -> list[str]:
    directory = _check_path(log_dir or CADDY_LOG_DIR)
    return [
        "\t# DP-6: the access log keeps no query strings and no headers.",
        "\tlog {",
        f"\t\toutput file {directory}/{name}.log {{",
        "\t\t\troll_size 10mb",
        "\t\t\troll_keep 5",
        "\t\t}",
        *_filter_lines(2),
        "\t}",
    ]


def _header_lines(site: Site, regenerate: str, summary: str) -> list[str]:
    return [
        f"{GENERATED_MARKER}; do not edit. {regenerate}",
        SITE_META_PREFIX + json.dumps(site.meta(), sort_keys=True, separators=(",", ":")),
        f"# {summary}",
    ]


# ── what a site block serves ──


@dataclass(frozen=True)
class Site:
    """What a site block serves. Its metadata line records this, so it can be regenerated."""

    name: str
    kind: str  # "service" (through the gateway) or "direct" (straight to a local web app)
    guard: bool = False  # service: also served on the public host
    upstream: str | None = None  # direct: "127.0.0.1:3000" or "[::1]:3000"

    def __post_init__(self) -> None:
        _check_name(self.name)
        if self.kind == "service":
            if not isinstance(self.guard, bool) or self.upstream is not None:
                raise ValueError(f"service {self.name!r}: bad metadata")
        elif self.kind == "direct":
            if self.guard is not False or not isinstance(self.upstream, str):
                raise ValueError(f"direct site {self.name!r}: bad metadata")
            _parse_upstream(self.upstream)
        else:
            raise ValueError(f"site {self.name!r}: unknown kind {self.kind!r}")

    def meta(self) -> dict[str, Any]:
        if self.kind == "service":
            return {"kind": "service", "name": self.name, "guard": self.guard}
        return {"kind": "direct", "name": self.name, "upstream": self.upstream}

    @classmethod
    def from_meta(cls, meta: Any) -> Site:
        if not isinstance(meta, dict) or set(meta) - {"kind", "name", "guard", "upstream"}:
            raise ValueError(f"bad site metadata {meta!r}")
        return cls(name=meta.get("name"), kind=meta.get("kind"),
                   guard=meta.get("guard", False), upstream=meta.get("upstream"))

    @property
    def port(self) -> int:
        """The listener it is served on: direct sites on the direct one (DP-3)."""
        return DIRECT_PORT if self.kind == "direct" else CADDY_PORT

    def hosts(self, domain: str) -> list[str]:
        """The host names its block serves: the loopback one, and the public one where it is public."""
        return _site_hosts(self.name, domain, public=self.kind == "direct" or self.guard)

    def describe(self, domain: str) -> str:
        hosts = " and ".join(self.hosts(domain))
        if self.kind == "direct":
            return (f"direct site -> {self.upstream}: {hosts}, on the direct listener (:{DIRECT_PORT}). It "
                    f"bypasses the gateway: keep :{DIRECT_PORT} out of the agent's allow-list (DP-3)")
        if not self.guard:
            return f"service, unguarded: {hosts} only"
        return f"service, guarded: {hosts}" + ("" if domain else " (no public domain)")


# ── generators ──


def generate_direct_site_block(
    name: str,
    domain: str,
    target_port: int,
    caddy_port: int = DIRECT_PORT,
    listen: Sequence[int] | None = None,
    target_host: str = "127.0.0.1",
    log_dir: Path | None = None,
) -> str:
    """Generate a Caddy site block for a direct (non-MCP) web app on loopback.

    Proxies straight to ``target_host:target_port``, around the gateway, its guard and its
    audit log, on the local host and on the public one. It is served on the direct listener
    (:7003; ``listen`` is its file descriptors, by default the installed unit's), never on the
    gateway's: the agent's egress allow-list names :7001 only, so the agent cannot reach the app
    through Caddy (DP-3). No rate limiting on MCP endpoints (there are none).
    """
    _check_name(name)
    _check_port(caddy_port)
    upstream = upstream_address(target_host, target_port)
    site = Site(name=name, kind="direct", upstream=upstream)
    hosts = _site_addresses(name, domain, caddy_port, public=True)

    lines = [
        *_header_lines(
            site,
            f"Recreate with `webspec-ctl add {name} --direct --url http://{upstream}`.",
            f"Direct site {name!r}: proxies to {upstream}, bypassing the gateway, on the direct listener.",
        ),
        f"{hosts} {{",
        *_common_site_lines(name, _direct_binds(listen), f"the direct listener (:{DIRECT_PORT})"),
        "",
        "\t# DP-3: this bypasses the gateway, so it is never on the gateway's listener (:7001), the",
        f"\t# only one the agent's allow-list names. cloudflared sends its public host to :{DIRECT_PORT}.",
        "",
        "\t# By IP: a name could resolve to ::1 first, where any local user could listen.",
        f"\treverse_proxy {upstream}",
        "",
        *_log_lines(name, log_dir),
        "}",
    ]
    return "\n".join(lines) + "\n"


def client_key_rules() -> list[tuple[str, str]]:
    """The ``map`` from Cf-Connecting-Ip to the key a tunneled client is counted by; first match wins.

    An IPv6 client is counted by its /64, written as its first four groups and ``::/64``: one
    subscriber holds at least a /64, and would otherwise take a fresh allowance from each of its
    addresses (F28). ``::`` may stand for zero groups inside the /64 too, so each place it can
    take has a rule, and the zeros it hides are written out. Anything else, an IPv4 address
    first of all, is its own key. A client that holds a larger prefix (a /56 or a /48) still
    gets one allowance per /64 in it: rate-limit at Cloudflare's edge as well.
    """
    hexgroup = "[0-9a-fA-F]{1,4}"
    group = f"({hexgroup})"

    def captured(first: int, count: int) -> list[str]:
        return [f"${{{first + i}}}" for i in range(count)]

    rules = [(f"^{group}:{group}:{group}:{group}:", ":".join(captured(1, 4)) + "::/64")]
    # "::" after a < 4 groups, and at most four after it: the rest of the /64 is zeros.
    for a in range(4):
        head = ":".join([group] * a) + "::"
        rules.append((f"^{head}(?:{hexgroup}(?::{hexgroup}){{0,3}})?$",
                      ":".join(captured(1, a) + ["0"] * (4 - a)) + "::/64"))
    # "::" after a groups, and b >= 5 after it: the first b - 4 of those are in the /64.
    for b in (5, 6, 7):
        for a in range(8 - b):
            head = ":".join([group] * a) + "::"
            tail = ":".join([group] * (b - 4) + [hexgroup] * 4)
            rules.append((f"^{head}{tail}$",
                          ":".join(captured(1, a) + ["0"] * (8 - a - b) + captured(a + 1, b - 4)) + "::/64"))
    rules.append(("^(.*)$", "${1}"))
    return rules


def generate_site_block(
    name: str,
    domain: str,
    gateway_port: int = 7002,
    guard: bool = False,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    rate_window: str = DEFAULT_RATE_WINDOW,
    caddy_port: int = CADDY_PORT,
    listen: Sequence[int] | None = None,
    log_dir: Path | None = None,
    nonce_rate_limit: int | None = None,
) -> str:
    """Generate a Caddy site block for a service.

    Returns the Caddy config text for one service subdomain, on the gateway's listener (:7001;
    ``listen`` is its file descriptors, by default the installed unit's). /__nonce and
    /__challenge are counted in zones of their own, with ``nonce_rate_limit`` requests per
    window (twice ``rate_limit`` by default).
    """
    _check_name(name)
    _check_port(gateway_port)
    _check_port(caddy_port)
    nonce_limit = 2 * rate_limit if nonce_rate_limit is None else nonce_rate_limit
    for limit in (rate_limit, nonce_limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"rate limit {limit!r} must be a positive integer")
    if not _DURATION.fullmatch(rate_window):
        raise ValueError(f"rate_window {rate_window!r} is not a duration such as 1m")
    site = Site(name=name, kind="service", guard=bool(guard))
    # Unguarded services are localhost-only (see public-guard invariant).
    hosts = _site_addresses(name, domain, caddy_port, public=site.guard)
    tunneled, local = f"header {TUNNEL_HEADER} *", f"not header {TUNNEL_HEADER} *"
    nonce = f"path {' '.join(NONCE_PATHS)}"

    def zone(suffix: str, matchers: Sequence[str], key: str, events: int) -> list[str]:
        # Zone names are global in caddy-ratelimit. "_" never occurs in a service name.
        return [
            f"\t\tzone {name}_{suffix} {{",
            "\t\t\tmatch {",
            *(f"\t\t\t\t{matcher}" for matcher in matchers),
            "\t\t\t}",
            f"\t\t\tkey {key}",
            f"\t\t\tevents {events}",
            f"\t\t\twindow {rate_window}",
            "\t\t}",
        ]

    lines = [
        *_header_lines(
            site,
            "Regenerate with `webspec-ctl caddy-sync`.",
            f"Service {name!r}, "
            + ("guarded: served on the local and the public host." if site.guard
               else "unguarded: served on the local host only."),
        ),
        f"{hosts} {{",
        *_common_site_lines(name, _gateway_binds(listen), f"the gateway's listener (:{CADDY_PORT})"),
        "",
        "\t# Rate limits count every request. /__nonce and /__challenge have zones of their own,",
        "\t# with twice the allowance, as each tool call needs a nonce; Caddy's path matcher ignores",
        f"\t# case and cleans the path, so no spelling of them escapes a zone. A request with {TUNNEL_HEADER}",
        "\t# counts in the tunnel zones, per client: the /64 of an IPv6 address, or the IPv4",
        f"\t# address, in {TUNNEL_CLIENT_HEADER}. Any other request counts in the local zones, per source",
        "\t# address. This binds each client that comes through the tunnel, whose headers Cloudflare",
        "\t# sets. It binds no local process: Caddy cannot tell one from cloudflared, so it can send",
        "\t# both headers itself, to escape its own count or to spend a public client's allowance.",
        f"\tmap {{http.request.header.{TUNNEL_CLIENT_HEADER}}} {{{CLIENT_KEY}}} {{",
        *(f'\t\t~{pattern} "{key}"' for pattern, key in client_key_rules()),
        "\t}",
        "\trate_limit {",
        *zone("tunnel", (tunneled, f"not {nonce}"), f"{{{CLIENT_KEY}}}", rate_limit),
        *zone("nonce_tunnel", (tunneled, nonce), f"{{{CLIENT_KEY}}}", nonce_limit),
        *zone("local", (local, f"not {nonce}"), "{remote_host}", rate_limit),
        *zone("nonce_local", (local, nonce), "{remote_host}", nonce_limit),
        "\t}",
        "",
        "\t# The gateway listens on 127.0.0.1 only. Dial the IP, not localhost, so that a process",
        "\t# listening on [::1] at the same port cannot take this traffic. reverse_proxy passes",
        "\t# the Host header through unchanged, as the guard signs it (DP-5).",
        f"\treverse_proxy 127.0.0.1:{gateway_port}",
        "",
        *_log_lines(name, log_dir),
        "}",
    ]
    return "\n".join(lines) + "\n"


def _caddyfile_meta(domain: str, base: Mapping[str, Any] | None = None) -> str:
    """The Caddyfile's metadata line, recording ``domain``; other fields of ``base`` are kept."""
    if domain:
        _check_domain(domain)
    meta = {**(base or {}), "domain": domain}
    return CADDYFILE_META_PREFIX + json.dumps(meta, sort_keys=True, separators=(",", ":"))


def generate_global_caddyfile(
    caddy_port: int = CADDY_PORT,
    conf_dir: Path | None = None,
    admin_socket: Path | None = None,
    listen: Listeners | None = None,
    domain: str = "",
    direct_port: int = DIRECT_PORT,
) -> str:
    """Generate /etc/caddy/Caddyfile: global options, the site-block import, and the catch-alls.

    ``listen`` is the socket unit's layout (by default the installed one): the gateway's
    listener and the direct one each get a catch-all. The header records ``domain``, the public
    domain the site blocks serve, and the layout, for webspec-ctl.
    """
    _check_port(caddy_port)
    _check_port(direct_port)
    conf = _check_path(conf_dir or CADDY_CONF_DIR)
    socket_path = _check_path(admin_socket or CADDY_ADMIN_SOCKET)
    listeners = installed_listeners() if listen is None else _check_listeners(listen)
    gateway_binds, direct_binds = _gateway_binds(listeners.gateway), _direct_binds(listeners.direct)

    lines = [
        f"{GENERATED_MARKER}.generate_global_caddyfile() via gateway/tools/setup-caddy.sh.",
        _caddyfile_meta(domain, {"listeners": listeners.name}),
        f"# Do not edit; re-run the script. Site blocks live in {conf}/ and are written by",
        "# `webspec-ctl add | rm | caddy-sync`, which serve the public domain recorded above",
        "# unless WEBSPEC_DOMAIN (or `caddy-sync --no-public`) says otherwise.",
        "#",
        f"#   cloudflared -> 127.0.0.1:{caddy_port}, [::1]:{caddy_port} (caddy-webspec.socket) -> Caddy",
        "#     -> gateway (127.0.0.1, WEBSPEC_INTERNAL_PORT)",
        f"#   cloudflared -> 127.0.0.1:{direct_port}, [::1]:{direct_port} (caddy-webspec.socket) -> Caddy",
        "#     -> direct sites (`webspec-ctl add --direct`), around the gateway",
        "{",
        "\t# DP-9: Caddy opens no listening socket of its own. systemd binds 127.0.0.1 and [::1]",
        f"\t# at :{caddy_port} and :{direct_port} (caddy-webspec.socket), passes them as file descriptors",
        "\t# 3 to 6, and keeps them bound while Caddy restarts, so no other local process can take",
        "\t# a port. Every site block binds its listener's sockets explicitly.",
        f"\tdefault_bind {gateway_binds}",
        "",
        "\t# The admin API can rewrite this configuration, so it does not listen on TCP, where",
        "\t# any local process could reach it. It listens on a unix socket in the caddy user's",
        "\t# 0700 runtime directory; reload with `caddy reload --config` as root or caddy.",
        "\t# Caddy warns \"admin endpoint on open interface; host checking disabled\" for every",
        "\t# unix socket, which is expected: the socket's file permissions control access.",
        f"\tadmin unix/{socket_path}|{CADDY_ADMIN_SOCKET_MODE}",
        "\tpersist_config off",
        "",
        "\t# TLS terminates at Cloudflare; Caddy serves plain HTTP/1.1 on loopback.",
        "\tauto_https off",
        "\tservers {",
        "\t\tprotocols h1",
        "\t}",
        "",
        "\t# DP-6: error entries (a 502 while the gateway is down, for example) include the",
        "\t# request, and go to the default logger, so it is filtered like the access logs.",
        "\tlog {",
        "\t\toutput stderr",
        *_filter_lines(2),
        "\t}",
        "}",
        "",
        f"import {conf}/*{_SUFFIX}",
        "",
        "# DP-5 catch-all of the gateway's listener: a Host without a site block is never forwarded.",
        f"http://:{caddy_port} {{",
        f"\tbind {gateway_binds}",
        '\trespond "Unknown service" 421 {',
        "\t\tclose",
        "\t}",
        "}",
        "",
        "# The same for the direct listener, which only direct sites use (DP-3).",
        f"http://:{direct_port} {{",
        f"\tbind {direct_binds}",
        '\trespond "Unknown service" 421 {',
        "\t\tclose",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def render_site(
    site: Site,
    domain: str,
    *,
    gateway_port: int = 7002,
    caddy_port: int = CADDY_PORT,
    listen: Listeners | None = None,
    log_dir: Path | None = None,
) -> str:
    """Generate the block for ``site`` with the current generator, on its listener of ``listen``.

    ``caddy_port`` is the port of the gateway's listener; a direct site is on :data:`DIRECT_PORT`.
    """
    listeners = installed_listeners() if listen is None else _check_listeners(listen)
    if site.kind == "direct":
        host, port = _parse_upstream(site.upstream or "")
        return generate_direct_site_block(site.name, domain, target_port=port, listen=listeners.direct,
                                          target_host=host, log_dir=log_dir)
    return generate_site_block(site.name, domain, gateway_port=gateway_port, guard=site.guard,
                               caddy_port=caddy_port, listen=listeners.gateway, log_dir=log_dir)


# ── files on disk ──


def _is_root() -> bool:
    return os.geteuid() == 0


def _trusted_uids() -> frozenset[int]:
    return frozenset({0, os.geteuid()})


_MAX_FILE = 1 << 20  # no file this module reads or copies is anywhere near 1 MiB


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    """The contents and status of ``path``, which must be a regular file.

    A symbolic link is not followed, and a FIFO is never waited on. Raises FileNotFoundError if
    there is nothing at ``path``, and another OSError if it is not a regular file of at most
    1 MiB or cannot be read.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(_MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_FILE:
        raise OSError(errno.EFBIG, "larger than 1 MiB", str(path))
    return data, st


def _read_trusted_block(path: Path) -> tuple[Site, str] | None:
    """What a trusted site block serves, and its text; None if the file is not trusted (read_site)."""
    if not path.name.endswith(_SUFFIX):
        return None
    try:
        data, st = _read_regular(path)
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if st.st_uid not in _trusted_uids() or st.st_mode & 0o022:
        return None
    lines = text.split("\n", 2)
    if len(lines) < 3 or not lines[0].startswith(GENERATED_MARKER) or not lines[1].startswith(SITE_META_PREFIX):
        return None
    try:
        site = Site.from_meta(json.loads(lines[1][len(SITE_META_PREFIX):]))
    except (ValueError, TypeError):
        return None
    return (site, text) if site.name == path.name[: -len(_SUFFIX)] else None


def read_site(path: Path) -> Site | None:
    """Return what a trusted site block serves, or None if the file is not trusted.

    Trusted: a regular file (not a symlink, FIFO or directory), owned by root or by the user
    running this code, writable by its owner only, whose first two lines are the marker and
    the metadata of a site with the file's own name.
    """
    block = _read_trusted_block(path)
    return None if block is None else block[0]


def live_site(name: str, conf_dir: Path | None = None) -> Site | None:
    """What the live block of ``name`` serves, if that block is trusted (:func:`read_site`)."""
    return read_site((conf_dir or CADDY_CONF_DIR) / f"{_check_name(name)}{_SUFFIX}")


def site_block_exists(name: str, conf_dir: Path | None = None) -> bool:
    """Whether Caddy imports an entry named for ``name``, whatever it is (a link, a hand-made file).

    False for a name that is not a DNS label, which no block can serve.
    """
    return is_label(name) and os.path.lexists((conf_dir or CADDY_CONF_DIR) / f"{name}{_SUFFIX}")


def _served_addresses(text: str) -> list[tuple[str, int]]:
    """The (host, port) pairs that a generated block's address line serves, in order; [] if none parse."""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.endswith(" {"):
            return []
        pairs = []
        for address in line[:-2].split(", "):
            m = re.fullmatch(r"http://([a-z0-9.-]+):(\d{1,5})", address)
            if not m:
                return []
            pairs.append((m.group(1), int(m.group(2))))
        return pairs
    return []


def _served_hosts(text: str) -> list[str]:
    """The host names that a generated block's address line serves, in order; [] if none parse."""
    return [host for host, _ in _served_addresses(text)]


# A site address in a Caddyfile that this module did not write: [scheme://]host[:port][/path].
_ANY_ADDRESS = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?P<host>\[[0-9A-Fa-f:.]+\]|[^\s:/,{}\[\]\"'`]*)"
                          r"(?::(?P<port>\d{1,5}))?(?:/[^\s{}]*)?")
_ANY_HOST = re.compile(r"(?:\*\.)?(?:[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?\.)*[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
                       r"|\*|\[[0-9a-f:.]+\]|\d{1,3}(?:\.\d{1,3}){3}")


def caddyfile_sites(text: str, known_imports: Iterable[str] = ()) -> tuple[list[tuple[str, int | None]], int]:
    """The sites a Caddyfile declares at its top level: ([(host, port)], how many could not be read).

    For files this module did not write (an earlier setup's blocks, a hand-made Caddyfile), so
    it reads the layout leniently but returns only lower-case host names, wildcards and IP
    literals, with the port where one is given. An address without a host (a catch-all such
    as ``:7001``) serves no particular host and is left out. Snippets and the global options
    block declare no site. A top-level ``import`` can declare any: it counts as unreadable,
    unless it is one of ``known_imports`` (whole lines, such as the import of conf.d, whose
    files the caller reads itself).
    """
    sites: list[tuple[str, int | None]] = []
    unreadable = 0
    depth = 0
    pending: list[str] = []
    known = {" ".join(line.split()) for line in known_imports}
    for raw in text.splitlines():
        line = re.sub(r"(?:^|\s)#.*", "", raw).strip()
        if not line:
            continue
        if depth == 0 and not pending and line.split()[0] == "import":
            unreadable += " ".join(line.split()) not in known
            continue
        if depth == 0:
            if line.endswith(","):  # an address list that goes on on the next line
                pending.append(line)
                continue
            if line.endswith("{"):
                tokens = " ".join([*pending, line[:-1]]).replace(",", " ").split()
                if tokens and not tokens[0].startswith("("):  # "(name) {" is a snippet
                    for token in tokens:
                        m = _ANY_ADDRESS.fullmatch(token)
                        host = m.group("host").lower().rstrip(".") if m else None
                        if host is None or (host and not _ANY_HOST.fullmatch(host)):
                            unreadable += 1
                        elif host:
                            sites.append((host, int(m.group("port")) if m.group("port") else None))
            pending = []
        depth = max(0, depth + line.count("{") - line.count("}"))
    return sites, unreadable


def scan_conf_dir(conf_dir: Path | None = None) -> tuple[dict[str, Site], list[Path]]:
    """Return the trusted site blocks by name, and every other ``*.caddy`` entry Caddy would import."""
    d = conf_dir or CADDY_CONF_DIR
    trusted: dict[str, Site] = {}
    untrusted: list[Path] = []
    if not d.is_dir():
        return trusted, untrusted
    for path in sorted(d.iterdir()):
        if not path.name.endswith(_SUFFIX):
            continue  # not matched by the import glob
        site = read_site(path)
        if site is None:
            untrusted.append(path)
        else:
            trusted[site.name] = site
    return trusted, untrusted


@dataclass
class SitePlan:
    """The site blocks a configuration should hold, derived from the gateway config and conf.d."""

    sites: dict[str, Site]  # blocks to (re)write
    stale: list[str]  # trusted service blocks to remove: no longer in the gateway config
    untrusted: list[Path]  # files this module did not write, or that others can write
    notes: list[str]
    adopted: list[str] = field(default_factory=list)  # direct sites regenerated from untrusted blocks


_LEGACY_UPSTREAM = re.compile(r"\s*reverse_proxy\s+(?:http://)?(localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})\s*")


def legacy_direct_site(path: Path, gateway_port: int = 7002) -> Site | None:
    """The direct site that a block of an earlier setup served, to regenerate it (F30); None if none.

    Such a block (``webspec-ctl add --direct`` before this module marked its blocks) proxies to
    one loopback port that is not the gateway's. The file is untrusted, as setup-caddy.sh moves
    it out: only its name, which must be a DNS label, and that port are taken from it. A block
    that proxies to the gateway is a service block, which the gateway config decides on.
    """
    name = path.name[: -len(_SUFFIX)] if path.name.endswith(_SUFFIX) else ""
    if not is_label(name):
        return None
    try:
        text = _read_regular(path)[0].decode("utf-8", errors="replace")  # no links, no FIFOs
    except OSError:
        return None
    upstreams = {(m.group(1), int(m.group(2))) for line in text.splitlines()
                 if (m := _LEGACY_UPSTREAM.fullmatch(line))}
    if len(upstreams) != 1:
        return None
    host, port = upstreams.pop()
    if reserved_port_owner(port, gateway_port) or not 0 < port < 65536:
        return None  # an old service block, or a loop through Caddy
    # localhost means 127.0.0.1, as for `webspec-ctl add --direct --port`.
    return Site(name=name, kind="direct", upstream=upstream_address("::1" if host == "[::1]" else "127.0.0.1", port))


def plan_sites(
    services: Mapping[str, Any] | None,
    conf_dir: Path | None = None,
    *,
    gateway_port: int = 7002,
    adopt_legacy: bool = False,
) -> SitePlan:
    """Plan the site blocks: one per configured service, plus the trusted direct sites.

    ``services`` maps names to entries with a ``guard`` attribute; None means the gateway
    config is not available. A trusted service block that the config no longer lists is
    stale, but only a config that lists at least one service can say so: with none (or no
    config), every trusted block is kept and regenerated from its metadata, so a missing,
    unreadable or emptied config can never wipe the proxy. Direct sites are not in the
    gateway config and are always kept, unless a service of the same name replaces one.
    ``adopt_legacy`` (setup-caddy.sh) also regenerates the direct sites of untrusted blocks
    that an earlier setup wrote (:func:`legacy_direct_site`); their files are still moved out.
    """
    trusted, untrusted = scan_conf_dir(conf_dir)
    sites: dict[str, Site] = {}
    stale: list[str] = []
    notes: list[str] = []
    adopted: list[str] = []
    for name, entry in sorted((services or {}).items()):
        try:
            sites[name] = Site(name=name, kind="service", guard=bool(entry.guard))
        except ValueError as exc:
            # The gateway rejects such a host anyway (hostgrammar); one bad name must not stop the rest.
            notes.append(f"skipped {name!r}: {exc}")
            continue
        old = trusted.get(name)
        if old is not None and old.kind == "direct":
            notes.append(f"{name}: the gateway service replaces the direct site to {old.upstream}")
    for name, site in sorted(trusted.items()):
        if name in sites:
            continue
        if site.kind == "service" and services:
            stale.append(name)
            continue
        sites[name] = site
        if site.kind == "service":
            notes.append(f"{name}: kept, because the gateway config lists no services")
        elif owner := reserved_port_owner(_parse_upstream(site.upstream or "")[1], gateway_port):
            # Written before add --direct refused these (P12): kept, as every direct site is.
            notes.append(f"{name}: the direct site proxies to {site.upstream}, {owner}, not to an app. Remove it "
                         f"(webspec-ctl rm {name}), then add it again with the port its app listens on")
    for path in untrusted if adopt_legacy else ():
        site = legacy_direct_site(path, gateway_port)
        if site is None or site.name in sites:
            continue  # not a direct site, or a gateway service of that name replaces it
        sites[site.name] = site
        adopted.append(site.name)
        notes.append(f"{site.name}: the direct site to {site.upstream} that the previous setup served is "
                     f"regenerated, on the direct listener (:{DIRECT_PORT})")
    return SitePlan(sites=sites, stale=stale, untrusted=untrusted, notes=notes, adopted=adopted)


@dataclass
class HostReport:
    """What becomes of the hosts Caddy serves today under a plan (setup-caddy.sh, F9)."""

    before: dict[str, set[int | None]]  # host -> the ports it is served on today (None: not given)
    after: dict[str, int]  # host -> the port it is served on afterwards
    unreadable: list[str] = field(default_factory=list)  # files whose hosts could not all be read

    @property
    def dropped(self) -> list[str]:
        """Hosts served today and not afterwards."""
        return sorted(host for host in self.before if host not in self.after)

    @property
    def moved(self) -> list[str]:
        """Hosts that move to the direct listener: direct sites that leave :7001 for :7003 (DP-3)."""
        return sorted(host for host, ports in self.before.items()
                      if self.after.get(host) == DIRECT_PORT and None not in ports and DIRECT_PORT not in ports)

    @property
    def left_direct(self) -> list[str]:
        """Hosts that leave the direct listener for the gateway's: a gateway service replaces a direct site."""
        return sorted(host for host, ports in self.before.items()
                      if host in self.after and self.after[host] != DIRECT_PORT and DIRECT_PORT in ports)

    def refused(self, allow_shrink: bool) -> bool:
        """True if the plan drops hosts, or may (some files cannot be read), and that was not allowed."""
        return bool(self.dropped or self.unreadable) and not allow_shrink


def served_today(conf_dir: Path | None = None, caddyfile: Path | None = None) -> tuple[dict[str, set[int | None]], list[str]]:
    """The hosts Caddy serves today, with their ports, and the files whose hosts could not be read.

    Every ``*.caddy`` entry of conf.d, whoever wrote it, and the sites of the Caddyfile itself
    (an earlier setup's, or one edited by hand), whose import of conf.d is read here. An entry
    that is not a regular UTF-8 file cannot be read: a symlink is not followed. Neither can an
    address that is not a host name (a placeholder, say) or an import of other files.
    """
    d = conf_dir or CADDY_CONF_DIR
    cf = caddyfile or CADDYFILE
    paths = [cf] if os.path.lexists(cf) else []
    if d.is_dir():
        paths += sorted(p for p in d.iterdir() if p.name.endswith(_SUFFIX))
    hosts: dict[str, set[int | None]] = {}
    unreadable: list[str] = []
    for path in paths:
        try:
            text = _read_regular(path)[0].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(f"{str(path)!r} (not a regular UTF-8 file)")  # repr: names may hold escapes
            continue
        sites, unknown = caddyfile_sites(text, [f"import {d}/*{_SUFFIX}"] if path == cf else [])
        for host, port in sites:
            hosts.setdefault(host, set()).add(port)
        if unknown:
            unreadable.append(f"{str(path)!r} ({unknown} address(es) or import(s) that name no host)")
    return hosts, unreadable


def host_report(
    plan: SitePlan,
    domain: str,
    *,
    caddy_port: int = CADDY_PORT,
    conf_dir: Path | None = None,
    caddyfile: Path | None = None,
) -> HostReport:
    """Compare the hosts Caddy serves today with the ones ``plan`` serves (:class:`HostReport`)."""
    before, unreadable = served_today(conf_dir, caddyfile)
    after = {host: DIRECT_PORT if site.kind == "direct" else caddy_port
             for site in plan.sites.values() for host in site.hosts(domain)}
    return HostReport(before, after, unreadable)


def _atomic_write(path: Path, content: str | bytes, mode: int = 0o644,
                  owner: tuple[int, int] | None = None) -> None:
    """Write ``path`` as a new file and rename it into place.

    The rename replaces whatever was there, a symlink included, without following it, and
    leaves a fresh file owned by the writer: an older file's owner or links do not survive.
    ``owner`` (uid, gid) gives it another owner instead, as root only: a restored file.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # Written as root, read by Caddy as the caddy user: a restrictive root umask must not
        # leave the file unreadable. It holds no secrets.
        os.fchmod(fd, mode)
        if owner is not None and _is_root():
            os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_site_block(name: str, content: str, conf_dir: Path | None = None) -> Path:
    """Write a site block to the Caddy conf.d directory. Returns the file path."""
    d = conf_dir or CADDY_CONF_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_check_name(name)}{_SUFFIX}"
    if p.is_symlink():
        logger.warning("Replacing the symlink %s with a regular file (its target is not touched)", p)
    _atomic_write(p, content)
    return p


def remove_site_block(name: str, conf_dir: Path | None = None) -> None:
    """Remove a service's Caddy config file (a symlink itself, never its target)."""
    d = conf_dir or CADDY_CONF_DIR
    p = d / f"{name}{_SUFFIX}"
    if p.is_symlink() or p.exists():
        p.unlink()
        logger.info("Removed Caddy config: %s", p)


def _services(registry: ServiceRegistry | Mapping[str, ServiceEntry]) -> dict[str, Any]:
    if isinstance(registry, Mapping):
        return dict(registry)
    entries = ((name, registry.get(name)) for name in registry.names())
    return {name: entry for name, entry in entries if entry is not None}


@dataclass
class SyncResult:
    """What a sync changes: site block name -> its new text, or None to remove it."""

    changes: dict[str, str | None]
    added: list[str] = field(default_factory=list)  # no file of that name yet
    updated: list[str] = field(default_factory=list)  # the file's text changes
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)  # updated block -> hosts it stops serving
    moved: dict[str, list[str]] = field(default_factory=dict)  # updated block -> hosts now on the direct listener
    # updated block -> hosts it served on the direct listener and no longer does: a gateway service
    # replaced a direct site. Those still served are on the gateway's listener; the others are dropped.
    left_direct: dict[str, list[str]] = field(default_factory=dict)
    plan: SitePlan | None = None


def _current_text(path: Path) -> str | None:
    """The text of the regular file at ``path``; None for anything else (missing, a link, ...)."""
    try:
        return _read_regular(path)[0].decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def plan_sync(
    registry: ServiceRegistry | Mapping[str, ServiceEntry],
    domain: str,
    gateway_port: int = 7002,
    caddy_port: int = CADDY_PORT,
    conf_dir: Path | None = None,
    listen: Listeners | None = None,
) -> SyncResult:
    """Plan the site blocks of every service, plus the trusted direct sites; change nothing.

    A block is (re)written for every service and trusted direct site whose text differs from
    the file's. Only trusted service blocks that the registry no longer lists are removed, and
    none at all when the registry is empty (see :func:`plan_sites`). Files this module did not
    write are left alone, unless a service of the same name replaces one; setup-caddy.sh moves
    them out. The blocks bind the sockets of ``listen``, by default the installed socket unit's
    (:func:`installed_listeners`).
    """
    d = conf_dir or CADDY_CONF_DIR
    services = _services(registry)
    if not services:
        logger.warning("The gateway config lists no services; no site block is removed.")
    plan = plan_sites(services, d, gateway_port=gateway_port)
    for note in plan.notes:
        logger.warning("%s", note)
    for path in plan.untrusted:
        if path.name[: -len(_SUFFIX)] not in plan.sites:
            logger.warning("Left %s alone: webspec.caddy did not write it, or others can write it. "
                           "Caddy still imports it; gateway/tools/setup-caddy.sh moves such files out "
                           "of %s.", path, d)
    listeners = installed_listeners() if listen is None else _check_listeners(listen)
    result = SyncResult(changes={}, plan=plan)
    for name, site in sorted(plan.sites.items()):
        try:
            content = render_site(site, domain, gateway_port=gateway_port, caddy_port=caddy_port, listen=listeners)
        except ValueError as exc:
            logger.error("Skipping Caddy site block for %r: %s", name, exc)
            continue
        path = d / f"{name}{_SUFFIX}"
        if not os.path.lexists(path):
            result.added.append(name)
        else:
            old = _current_text(path)
            if old == content:
                result.unchanged.append(name)
                continue
            result.updated.append(name)
            new_addresses = dict(_served_addresses(content))
            old_addresses = _served_addresses(old or "")
            lost = [host for host, _ in old_addresses if host not in new_addresses]
            if lost:
                result.dropped[name] = lost
            # A direct site from before it had a listener of its own (DP-3): cloudflared must follow.
            moved = [host for host, port in old_addresses
                     if port != DIRECT_PORT and new_addresses.get(host) == DIRECT_PORT]
            if moved:
                result.moved[name] = moved
            # A direct site that a gateway service replaces (P10): cloudflared must stop sending its
            # public host to the direct listener, which no longer serves it. (A direct site that
            # changes domain stays one: its old hosts are dropped, and the domain's rules change.)
            left = [host for host, port in old_addresses if port == DIRECT_PORT] if site.kind == "service" else []
            if left:
                result.left_direct[name] = left
        result.changes[name] = content
    for name in plan.stale:
        result.changes[name] = None
        result.removed.append(name)
    return result


def sync_caddy_config(
    registry: ServiceRegistry | Mapping[str, ServiceEntry],
    domain: str,
    gateway_port: int = 7002,
    caddy_port: int = CADDY_PORT,
    conf_dir: Path | None = None,
    listen: Listeners | None = None,
) -> tuple[list[str], list[str]]:
    """Write the blocks :func:`plan_sync` plans, as they are; return the (added, removed) names.

    Nothing is validated and Caddy is not reloaded: this writes a configuration directory
    (tests, a staging copy). webspec-ctl changes the live one through :func:`apply_site_changes`.
    """
    d = conf_dir or CADDY_CONF_DIR
    d.mkdir(parents=True, exist_ok=True)
    result = plan_sync(registry, domain, gateway_port=gateway_port, caddy_port=caddy_port, conf_dir=d,
                       listen=listen)
    for name, content in result.changes.items():
        if content is None:
            remove_site_block(name, conf_dir=d)
        else:
            write_site_block(name, content, conf_dir=d)
    return sorted(result.added), sorted(result.removed)


# ── the live Caddyfile ──


class DomainError(ValueError):
    """The public domain cannot be determined without asking: set WEBSPEC_DOMAIN."""


@dataclass(frozen=True)
class GlobalCaddyfile:
    """The live Caddyfile, as far as webspec-ctl is concerned."""

    path: Path
    text: str
    trusted: bool  # a regular file, owned by root (or the user running this), writable by its owner only
    generated: bool  # written by setup-caddy.sh (generate_global_caddyfile): the host was migrated
    mode: int = 0o644


def read_caddyfile(path: Path | None = None) -> GlobalCaddyfile | None:
    """The live Caddyfile; None if there is none."""
    p = path or CADDYFILE
    try:
        data, st = _read_regular(p)
        text = data.decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return GlobalCaddyfile(p, "", trusted=False, generated=False)
    trusted = st.st_uid in _trusted_uids() and not st.st_mode & 0o022
    return GlobalCaddyfile(p, text, trusted=trusted, generated=text.startswith(GENERATED_MARKER),
                           mode=stat.S_IMODE(st.st_mode))


def _caddyfile_record(info: GlobalCaddyfile) -> dict[str, Any] | None:
    """The metadata line of a generated Caddyfile, parsed; None if it has none (an older one)."""
    lines = info.text.split("\n", 2)
    if len(lines) < 2 or not lines[1].startswith(CADDYFILE_META_PREFIX):
        return None
    try:
        meta = json.loads(lines[1][len(CADDYFILE_META_PREFIX):])
        if not isinstance(meta, dict) or not isinstance(meta.get("domain"), str):
            raise ValueError("no domain")
        if meta["domain"]:
            _check_domain(meta["domain"])
    except ValueError as exc:
        raise DomainError(f"{info.path} records no readable domain on its second line ({exc}); "
                          "set WEBSPEC_DOMAIN, or run gateway/tools/setup-caddy.sh again") from None
    return meta


def _caddyfile_fields(info: GlobalCaddyfile) -> dict[str, Any]:
    """The fields of a generated Caddyfile's metadata line, whatever they hold; {} if there are none."""
    lines = info.text.split("\n", 2)
    if len(lines) < 2 or not lines[1].startswith(CADDYFILE_META_PREFIX):
        return {}
    try:
        meta = json.loads(lines[1][len(CADDYFILE_META_PREFIX):])
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def recorded_listeners(info: GlobalCaddyfile) -> str | None:
    """The listener layout a generated Caddyfile binds (a key of :data:`LAYOUTS`); None if it records none.

    A Caddyfile from before direct sites had a listener of their own records none.
    """
    value = _caddyfile_fields(info).get("listeners")
    return value if isinstance(value, str) else None


def _with_recorded_domain(info: GlobalCaddyfile, domain: str) -> str:
    """The text of a generated Caddyfile whose header records ``domain``.

    The other fields of the record (the listener layout) are kept; an unreadable domain is
    replaced by the one given now.
    """
    lines = info.text.split("\n")
    line = _caddyfile_meta(domain, _caddyfile_fields(info))
    if len(lines) > 1 and lines[1].startswith(CADDYFILE_META_PREFIX):
        lines[1] = line
    else:
        lines.insert(1, line)
    return "\n".join(lines)


def recorded_domain(caddyfile: Path | None = None, conf_dir: Path | None = None) -> tuple[str | None, str]:
    """The public domain the live configuration records, and where; (None, "") if it records none.

    The header of the generated Caddyfile records it. A Caddyfile generated before the header
    did records it only in the public addresses of the trusted site blocks. On the layout from
    before setup-caddy.sh (a Caddyfile it did not generate), the blocks that layout left serve
    it (:func:`_legacy_domain`), so a first run of setup-caddy.sh keeps serving it (F9). Raises
    DomainError if the record cannot be read, or the blocks serve more than one domain.
    """
    info = read_caddyfile(caddyfile)
    d = conf_dir or CADDY_CONF_DIR
    if info is None:
        return None, ""
    if not info.generated:
        return _legacy_domain(d)
    if not info.trusted:
        return None, ""
    meta = _caddyfile_record(info)
    if meta is not None:
        return meta["domain"], f"recorded in {info.path}"
    found: set[str] = set()
    for path in sorted(d.iterdir()) if d.is_dir() else ():
        block = _read_trusted_block(path)
        if block is None:
            continue
        site, text = block
        prefix = f"{site.name}."
        found.update(host[len(prefix):] for host in _served_hosts(text)
                     if host.startswith(prefix) and host != f"{site.name}.localhost")
    if len(found) > 1:
        raise DomainError(f"the site blocks in {d} serve more than one public domain "
                          f"({', '.join(sorted(found))}); set WEBSPEC_DOMAIN")
    if found:
        return found.pop(), f"served by the site blocks in {d}"
    return None, ""


def _legacy_domain(conf_dir: Path) -> tuple[str | None, str]:
    """The public domain the blocks of the layout before setup-caddy.sh serve, and where.

    Those blocks are untrusted: a domain is taken from ``<name>.<domain>`` hosts of a block
    named ``<name>.caddy`` only, and must be a DNS name. More than one is a DomainError.
    """
    found: set[str] = set()
    for path in sorted(conf_dir.iterdir()) if conf_dir.is_dir() else ():
        name = path.name[: -len(_SUFFIX)] if path.name.endswith(_SUFFIX) else ""
        if not is_label(name):
            continue
        try:
            text = _read_regular(path)[0].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for host, _ in caddyfile_sites(text)[0]:
            rest = host[len(name) + 1:] if host.startswith(f"{name}.") else ""
            if rest and rest != "localhost" and all(is_label(part) for part in rest.split(".")):
                found.add(rest)
    if len(found) > 1:
        raise DomainError(f"the site blocks that the previous setup left in {conf_dir} serve more than one "
                          f"public domain ({', '.join(sorted(found))}); set WEBSPEC_DOMAIN")
    if found:
        return found.pop(), f"served by the site blocks that the previous setup left in {conf_dir}"
    return None, ""


# ── public domain ──


# (env file, key) pairs whose ignored `export KEY=` lines were warned about: once per run.
_WARNED_EXPORTS: set[tuple[str, str]] = set()


def read_env_file_value(path: Path, key: str, *, sourced: bool | None = None) -> str | None:
    """Return KEY's last value in the gateway's env file, as its consumer reads it; None if it is not set there.

    On Linux, the production gateway.env is a systemd EnvironmentFile: ``KEY=value`` lines only.
    systemd ignores an ``export KEY=value`` line, so the gateway never gets KEY from one: such a
    line is not counted, and draws a warning (P11). On macOS, the gateway.env that the daemon's
    /bin/sh sources, where ``export KEY=value`` sets KEY too (``sourced``, by default
    :func:`config_writer.env_file_sourced` of ``path``). The file is read as data, never
    sourced. Surrounding quotes are removed, as both do. An unreadable file counts as not
    setting it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if sourced is None:
        sourced = config_writer.env_file_sourced(path)
    value = None
    pattern = config_writer.env_assignment(key, sourced)
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            value = line[m.end():].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
    warning = config_writer.ignored_export_warning(key, path, sourced)
    if warning and (str(path), key) not in _WARNED_EXPORTS:
        _WARNED_EXPORTS.add((str(path), key))
        logger.warning("%s.", warning)
    return value


@dataclass(frozen=True)
class Domain:
    """The public domain the site blocks serve; empty for loopback names (*.localhost) only."""

    name: str
    source: str
    explicit: bool  # given for this run (WEBSPEC_DOMAIN, --no-public), not remembered or derived
    note: str = ""  # what the gateway's own setting says, where the operator should know it


def resolve_domain(
    environ: Mapping[str, str] | None = None,
    *,
    gateway_env: Path | None = None,
    production: bool | None = None,
    caddyfile: Path | None = None,
    conf_dir: Path | None = None,
) -> Domain:
    """The public domain for the site blocks, and where it came from.

    1. WEBSPEC_DOMAIN in the environment, even when empty (loopback names only).
    2. The domain the live configuration records (:func:`recorded_domain`), none included:
       setup-caddy.sh records the one it was given, and ``caddy-sync --no-public`` (or
       WEBSPEC_DOMAIN=) records none, so a routine webspec-ctl run through sudo, which loses
       WEBSPEC_DOMAIN, keeps serving exactly that. Before setup-caddy.sh first ran, the one the
       blocks of the earlier layout serve.
    3. Where nothing is recorded, on a production host (config_writer.is_production_host), a
       non-empty WEBSPEC_DOMAIN in /etc/webspec/gateway.env: the gateway's own setting.
    4. None.

    There is no built-in default: a domain this host does not serve must not appear in its
    configuration. Only WEBSPEC_DOMAIN or --no-public changes a recorded domain: gateway.env
    must keep the gateway's WEBSPEC_DOMAIN even while the public addresses are turned off, so
    it never puts them back by itself. If it names another public domain than the recorded
    one, the public hosts would reach a gateway that answers 404 for them: DomainError asks
    which is meant. Where they differ otherwise, the result's ``note`` says so.
    """
    env = os.environ if environ is None else environ
    if "WEBSPEC_DOMAIN" in env:
        value = env["WEBSPEC_DOMAIN"].strip()
        if value:
            _check_domain(value)
        return Domain(value, "the environment", explicit=True)
    recorded, where = recorded_domain(caddyfile, conf_dir)
    production = is_production_host() if production is None else production
    path = gateway_env or GATEWAY_ENV
    value = read_env_file_value(path, "WEBSPEC_DOMAIN") if production else None
    if value:
        _check_domain(value)
    if recorded is None:
        return Domain(value, str(path), explicit=False) if value else Domain("", "not set", explicit=False)
    if value and recorded and recorded != value:
        raise DomainError(f"{path} sets WEBSPEC_DOMAIN={value}, but the Caddy configuration serves "
                          f"{recorded} ({where}). Say which: run again with WEBSPEC_DOMAIN={value} "
                          f"(or {recorded}), or with WEBSPEC_DOMAIN= for *.localhost names only")
    note = ""
    if value and not recorded:
        note = (f"{path} sets WEBSPEC_DOMAIN={value}, but the public addresses are turned off; to serve "
                f"them again, run caddy-sync with WEBSPEC_DOMAIN={value}")
    elif production and recorded and value is not None and not value:
        note = (f"{path} sets WEBSPEC_DOMAIN to nothing, so unless the gateway gets it elsewhere, it answers "
                f"404 for the hosts of {recorded}: set WEBSPEC_DOMAIN={recorded} there, then restart it")
    return Domain(recorded, where, explicit=False, note=note)


def public_domain(
    environ: Mapping[str, str] | None = None,
    gateway_env: Path | None = None,
    **kwargs: Any,
) -> tuple[str, str]:
    """Return (domain, where it came from), as :func:`resolve_domain` decides."""
    domain = resolve_domain(environ, gateway_env=gateway_env, **kwargs)
    return domain.name, domain.source


def _system_unit_loaded(unit: str) -> bool:
    """True if systemd has a system unit of that name; False where it cannot say."""
    systemctl = shutil.which("systemctl", path=SAFE_PATH)
    if systemctl is None:
        return False
    try:
        result = subprocess.run([systemctl, "show", "--property=LoadState", "--value", unit],
                                capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT,
                                env={"PATH": SAFE_PATH, "LC_ALL": "C"}, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def gateway_domain_hint(domain: str) -> str:
    """How to have the running gateway serve ``domain`` too, with commands that work on this host.

    Caddy forwards a new public host at once, but the gateway reads WEBSPEC_DOMAIN only when it
    starts, and answers 404 for that host until it restarts (F31). Where the setting goes, and
    how to restart: the production unit (gateway/deploy/linux), another system unit of that name
    (the setup before gateway/deploy/), or the development unit, a user unit (gateway/systemd/).
    A production gateway.env that already sets ``domain`` needs only the restart.
    """
    _check_domain(domain)
    if is_production_host():
        where, restart = str(GATEWAY_ENV), "sudo systemctl restart webspec-gateway"
        if read_env_file_value(GATEWAY_ENV, "WEBSPEC_DOMAIN") == domain:
            return (f"The gateway reads WEBSPEC_DOMAIN only when it starts. {where} already sets "
                    f"WEBSPEC_DOMAIN={domain}: if the gateway has not been restarted since it was set there, "
                    f"restart it ({restart}); until then it answers 404 for the new hosts.")
    elif _system_unit_loaded("webspec-gateway.service"):
        where, restart = ("the environment of webspec-gateway.service (systemctl cat webspec-gateway)",
                          "sudo systemctl restart webspec-gateway")
    else:
        where = "~/.webspec/gateway.env of the user that runs the development gateway (gateway/systemd/)"
        restart = "as that user, systemctl --user restart webspec-gateway"
    return (f"The gateway reads WEBSPEC_DOMAIN only when it starts. Set WEBSPEC_DOMAIN={domain} in {where}, "
            f"then restart it ({restart}); until then it answers 404 for the new hosts.")


# ── the caddy that webspec-ctl runs (as root, through sudo) ──


class CaddyError(Exception):
    """webspec-ctl left Caddy's configuration as it was; the message says why and what to do."""


def _describe(path: Path) -> str:
    """Owner, group and mode of ``path`` itself (a link is not followed), for messages."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return exc.strerror or "cannot be examined"
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return f"{owner}:{group} {stat.filemode(st.st_mode)}"


def _others_can_modify(st: os.stat_result, *, link: bool = False) -> bool:
    """True if a user other than root could change this file or directory.

    The owner must be root. A symbolic link is judged by its owner alone, since its mode bits
    mean nothing; anything else must not be writable by its group or by others.
    """
    if st.st_uid != 0:
        return True
    return not link and bool(st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def unsafe_component(path: Path) -> Path | None:
    """The first path at or above ``path`` that a user other than root could modify; None if none.

    The test of unsafe_chain in gateway/deploy/macos/install.sh: every component as written,
    then every component of the physical path that the links lead to. It looks at owners and
    mode bits, not ACLs. A relative or unnormalized path is refused as a whole.
    """
    text = str(path)
    if not path.is_absolute() or os.path.normpath(text) != text:
        return path
    for current in (path, *path.parents):
        try:
            st = os.lstat(current)
        except OSError:
            return current
        if _others_can_modify(st, link=stat.S_ISLNK(st.st_mode)):
            return current
    real = Path(os.path.realpath(text))
    for current in (real, *real.parents):
        try:
            st = os.stat(current)
        except OSError:
            return current
        if _others_can_modify(st):
            return current
    return None


def caddy_binary() -> Path:
    """The caddy to run: WEBSPEC_CADDY_BIN, else /usr/local/bin/caddy. Never a name looked up on PATH."""
    explicit = os.environ.get(CADDY_BIN_ENV, "")
    return Path(explicit) if explicit else CADDY_BIN


def trusted_caddy() -> tuple[Path | None, str]:
    """(the caddy to run, "") or (None, why none may run).

    As root, the file and every directory above it must be root's and writable by root only:
    whoever could replace it would run code as root, then read the guard key (GD-5) and
    rewrite /etc/webspec (DP-1, DP-4).
    """
    path = caddy_binary()
    if not path.is_absolute():
        return None, (f"{CADDY_BIN_ENV}={str(path)!r} is not an absolute path; webspec-ctl does not "
                      "look caddy up on PATH")
    if not path.is_file():
        return None, (f"{path} does not exist; gateway/tools/setup-caddy.sh installs it there (or set "
                      f"{CADDY_BIN_ENV} to the absolute path of another caddy)")
    if _is_root():
        bad = unsafe_component(path)
        if bad is not None:
            return None, (f"{bad} ({_describe(bad)}) can be modified by a user other than root, who "
                          f"could replace {path}, so root does not run it. Make {bad} and every "
                          f"directory above it root's and writable by root only, or set {CADDY_BIN_ENV} "
                          "to a caddy that is")
    return path, ""


def _caddy_user() -> tuple[int, int, str] | None:
    """As root: (uid, gid, home) of caddy-webspec.service's user, which caddy then runs as.

    None when not root. Raises CaddyError if root finds no such user: setup-caddy.sh creates it.
    """
    if not _is_root():
        return None
    try:
        entry = pwd.getpwnam(CADDY_USER)
    except KeyError:
        raise CaddyError(f"there is no {CADDY_USER!r} user for Caddy to run as; set Caddy up with "
                         "gateway/tools/setup-caddy.sh") from None
    return entry.pw_uid, entry.pw_gid, entry.pw_dir


def _caddy_output(stdout: str, stderr: str) -> str:
    """The error messages in Caddy's output (JSON log lines and `Error:` lines), else its last lines."""
    lines = [line for line in (stderr + "\n" + stdout).splitlines() if line.strip()]
    errors = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            if line.startswith("Error:"):
                errors.append(line)
            continue
        if isinstance(entry, dict) and entry.get("level") in ("error", "fatal", "panic"):
            errors.append(str(entry.get("msg", "")) + (f": {entry['error']}" if entry.get("error") else ""))
    return "\n".join(errors or lines[-3:])[:4000]


_TIMED_OUT = "did not finish within"


def _run_caddy(binary: Path, args: Sequence[str], scratch: Path) -> tuple[bool, str]:
    """Run ``binary args``; return (whether it succeeded, its error output).

    caddy gets no part of the caller's environment. As root it runs as caddy-webspec.service's
    user, with the unit's XDG directories, as setup-caddy.sh validates: root never executes it,
    and the log files a validation opens belong to caddy. Otherwise its directories are in
    ``scratch``.
    """
    user = _caddy_user()
    if user is None:
        home, data, config = scratch, scratch / "data", scratch / "config"
        identity: dict[str, Any] = {}
    else:
        home, data, config = Path(user[2]), CADDY_STATE_DIR / "data", CADDY_STATE_DIR / "config"
        identity = {"user": user[0], "group": user[1], "extra_groups": []}
    env = {"PATH": SAFE_PATH, "HOME": str(home), "XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(config)}
    try:
        result = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=CADDY_TIMEOUT,
                                env=env, cwd="/", **identity)
    except subprocess.TimeoutExpired:
        return False, f"`caddy {args[0]}` {_TIMED_OUT} {CADDY_TIMEOUT} s"
    except OSError as exc:
        return False, f"cannot run {binary}: {exc}"
    return result.returncode == 0, _caddy_output(result.stdout or "", result.stderr or "")


def _scratch_dir() -> tempfile.TemporaryDirectory[str]:
    # As root, in /tmp: sticky, so no other user can rename what is staged there.
    return tempfile.TemporaryDirectory(prefix="webspec-caddy.", dir="/tmp" if _is_root() else None)


# ── reload ──


def reload_caddy(caddyfile: Path | None = None) -> bool:
    """Reload Caddy's configuration as it is on disk. Returns True on success.

    `caddy reload --config` takes the admin address from the config it loads (Caddy's
    DetermineAdminAPIAddress), which is the unix socket, and removes the `|mode` suffix
    before it dials. Only root and the caddy user can open the socket, so run webspec-ctl
    as root: ``sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync``. The caddy is
    :func:`trusted_caddy`'s; when there is none, nothing runs, and the reason and the command
    to reload by hand are logged. webspec-ctl itself goes through :func:`apply_site_changes`.
    """
    if _on_macos():
        logger.error("Not reloading Caddy: the macOS deployment ships no proxy for webspec-ctl to manage")
        return False
    binary, why = trusted_caddy()
    if binary is None:
        logger.error("Not reloading Caddy: %s. Once that is fixed, reload it as root: "
                     "systemctl reload caddy-webspec", why)
        return False
    try:
        with _scratch_dir() as tmp:
            ok, output = _run_caddy(binary, ["reload", "--config", str(caddyfile or CADDYFILE)], Path(tmp))
    except CaddyError as exc:
        logger.error("Not reloading Caddy: %s", exc)
        return False
    if ok:
        logger.info("Caddy reloaded successfully")
        return True
    logger.error("Caddy reload failed: %s", output)
    if "permission denied" in output.lower():
        logger.error("Only root and the caddy user can reach the admin socket %s; run "
                     "`sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync`.", CADDY_ADMIN_SOCKET)
    return False


# ── changing the live configuration (webspec-ctl) ──

MANAGED, ABSENT, REFUSED = "managed", "absent", "refused"


def _on_macos() -> bool:
    return sys.platform == "darwin"


def _import_line(conf_dir: Path) -> str:
    """The line of the generated Caddyfile that imports the site blocks."""
    return f"import {_check_path(conf_dir)}/*{_SUFFIX}"


def _layout_mismatch(caddyfile: Path, recorded: str, installed: Listeners) -> str | None:
    """Why a Caddyfile generated for the ``recorded`` layout misses the sockets systemd passes; None if it fits."""
    if recorded == installed.name:
        return None
    if recorded == DUAL.name and installed == IPV4_ONLY:
        effect = (": Caddy cannot start, as it binds fd/5 and fd/6, the [::1] sockets, which systemd passes only "
                  "while the kernel has IPv6")
    elif recorded == IPV4_ONLY.name and installed == DUAL:
        effect = (f": Caddy does not serve [::1]:{CADDY_PORT} and [::1]:{DIRECT_PORT}, which systemd holds, so "
                  "connections there wait unanswered")
    else:
        effect = ""
    return (f"{caddyfile} binds the sockets of the {recorded!r} layout, but {SOCKET_UNIT} passes "
            f"{', '.join(installed.addresses)} ({installed.name!r}){effect}. Run gateway/tools/setup-caddy.sh "
            "again: it writes the configuration for the sockets systemd passes on the kernel it runs on, so it "
            "must run again whenever IPv6 is turned on or off at boot")


def listener_mismatch(caddyfile: Path | None = None) -> str | None:
    """Why Caddy cannot serve the listeners caddy-webspec.socket passes with the live Caddyfile; None if it can.

    setup-caddy.sh writes the Caddyfile for the sockets systemd passes on the kernel it runs on
    (F26). They part when IPv6 is turned on or off at boot afterwards. A kernel booted with
    ipv6.disable=1 has systemd ignore the unit's [::1] lines, and a Caddyfile that binds fd/5
    and fd/6 cannot start (caddy-webspec.service's ExecStartPre says so at every start). Booted
    with IPv6 again, systemd holds [::1] too, and a Caddyfile written without IPv6 leaves those
    sockets unanswered. :func:`caddy_status` refuses changes then; ``webspec-ctl health``
    reports it. None as well where nothing can be compared: no generated Caddyfile that
    records its layout.
    """
    info = read_caddyfile(caddyfile)
    if info is None or not (info.trusted and info.generated):
        return None
    recorded = recorded_listeners(info)
    if recorded is None:
        return None
    try:
        installed = installed_listeners()
    except CaddyError as exc:
        return str(exc)
    return _layout_mismatch(info.path, recorded, installed)


def unheld_listeners() -> str | None:
    """Why nothing holds [::1]:7001 and [::1]:7003, though this kernel has IPv6; None if systemd does.

    caddy-webspec.socket lists them, and on a kernel with IPv6 systemd holds them, even while
    the configuration does not serve them (:func:`listener_mismatch`). It passes the IPv4
    sockets alone there only when something drops those lines: the IPv4-only drop-in that an
    earlier setup-caddy.sh wrote on a kernel without IPv6, which outlives a boot with IPv6, or
    an edit. Then any local process can listen on [::1]:7001 or [::1]:7003, and receive what
    local clients send there, guard tags included (DP-9, P2). This asks the kernel, which
    nothing that chooses the sockets to bind does (F26): it is about what nothing binds. None
    where systemd cannot say, or the kernel has no IPv6.
    """
    if _systemd_listen() != IPV4_ONLY.addresses or not ipv6_supported():
        return None
    if IPV4_ONLY_DROPIN.exists():
        cause = (f"{IPV4_ONLY_DROPIN}, which an earlier gateway/tools/setup-caddy.sh wrote on a kernel without "
                 "IPv6, drops its [::1] lines")
        fix = "Run gateway/tools/setup-caddy.sh again: it removes that drop-in, and systemd then holds them"
    else:
        cause = f"its [::1] lines are dropped (systemctl cat {SOCKET_UNIT} shows where)"
        fix = "Put them back, then run gateway/tools/setup-caddy.sh again"
    return (f"{SOCKET_UNIT} holds {' and '.join(IPV4_ONLY.addresses)} only, though this kernel has IPv6: {cause}. "
            f"Nothing holds [::1]:{CADDY_PORT} and [::1]:{DIRECT_PORT}, so any local process can listen there and "
            f"receive what local clients send to them (DP-9). {fix}")


def caddy_status(caddyfile: Path | None = None, conf_dir: Path | None = None) -> tuple[str, str]:
    """Whether webspec-ctl may change this host's Caddy configuration: (MANAGED | ABSENT | REFUSED, why).

    ABSENT: there is no WebSpec Caddy to change, so commands leave Caddy out (macOS, whose
    deployment ships no proxy, or a host without a Caddyfile). REFUSED: there is one, but a
    change now could leave a configuration that cannot start, or run code that another user
    can replace; ``why`` says what to do first.
    """
    cf = caddyfile or CADDYFILE
    d = conf_dir or CADDY_CONF_DIR
    if _on_macos():
        return ABSENT, ("macOS: the deployment ships no proxy, so webspec-ctl manages no Caddy here. "
                        "The proxy in front of the gateway is yours to run (DP-5, DP-6)")
    info = read_caddyfile(cf)
    if info is None:
        return ABSENT, (f"there is no {cf}: Caddy is not set up on this host. gateway/tools/setup-caddy.sh "
                        "sets it up and writes a site block for every service in the gateway config")
    if not info.trusted:
        return REFUSED, (f"{cf} ({_describe(cf)}) is not a regular file that only root can modify. "
                         "Run gateway/tools/setup-caddy.sh as root to rewrite it")
    if not info.generated:
        return REFUSED, (f"this host still runs the pre-hardening Caddy layout: {cf} was not written by "
                         "webspec.caddy, and that Caddy does not take its sockets from systemd, so webspec-ctl "
                         "leaves its configuration alone (a block for the new layout would keep it from "
                         "starting the next time it restarts). Migrate first: run gateway/tools/setup-caddy.sh "
                         "as root, from a checkout that only root can modify (see the script's header)")
    if _import_line(d) not in info.text.split("\n"):
        return REFUSED, f"{cf} does not import {d}/*{_SUFFIX}; run gateway/tools/setup-caddy.sh again"
    # The blocks bind what the installed socket unit passes (F26), and the Caddyfile's catch-alls
    # must bind the same: setup-caddy.sh installs the unit and the Caddyfile together.
    try:
        installed = installed_listeners()
    except CaddyError as exc:
        return REFUSED, str(exc)
    recorded = recorded_listeners(info)
    if recorded is None:
        return REFUSED, (f"{cf} was generated before direct sites had a listener of their own "
                         f"(127.0.0.1:{DIRECT_PORT}, DP-3). Run gateway/tools/setup-caddy.sh again, as root, "
                         "from a checkout that only root can modify, then run this again")
    mismatch = _layout_mismatch(cf, recorded, installed)
    if mismatch:
        return REFUSED, mismatch
    binary, why = trusted_caddy()
    if binary is None:
        return REFUSED, why
    try:
        _caddy_user()
    except CaddyError as exc:
        return REFUSED, str(exc)
    if not d.is_dir():
        return REFUSED, f"{d} does not exist; run gateway/tools/setup-caddy.sh again"
    if not os.access(d, os.W_OK):
        return REFUSED, (f"only root can change {d}: run webspec-ctl as root "
                         "(sudo /opt/webspec/venv/bin/webspec-ctl …)")
    return MANAGED, ""


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``directory``, so that two webspec-ctl runs never interleave."""
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _Snapshot:
    """A file as it was before a change, to put back if Caddy does not load the change."""

    path: Path
    data: bytes | None = None  # None: there was no file
    mode: int = 0o644
    owner: tuple[int, int] | None = None
    link: str | None = None  # it was a symbolic link to this


def _snapshot(path: Path) -> _Snapshot:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _Snapshot(path)
    if stat.S_ISLNK(st.st_mode):
        return _Snapshot(path, link=os.readlink(path))
    try:
        data, st = _read_regular(path)
    except OSError as exc:
        raise CaddyError(f"{path} is not a regular file ({exc.strerror}); move it out of {path.parent}") from None
    return _Snapshot(path, data=data, mode=stat.S_IMODE(st.st_mode), owner=(st.st_uid, st.st_gid))


def _restore(snapshots: Sequence[_Snapshot]) -> list[str]:
    """Put every file back as it was; return the ones that could not be, with why."""
    failed = []
    for snap in snapshots:
        try:
            if snap.link is not None:
                tmp = snap.path.with_name(f".{snap.path.name}.{secrets.token_hex(4)}.tmp")
                os.symlink(snap.link, tmp)
                os.replace(tmp, snap.path)
            elif snap.data is not None:
                _atomic_write(snap.path, snap.data, snap.mode, owner=snap.owner)
            elif os.path.lexists(snap.path):
                os.unlink(snap.path)
        except OSError as exc:
            failed.append(f"{snap.path}: {exc.strerror or exc}")
    return failed


def _write_new(path: Path, content: str | bytes, mode: int, owner: tuple[int, int] | None = None) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), mode)
        if owner is not None:
            os.fchown(f.fileno(), *owner)
        f.write(content.encode("utf-8") if isinstance(content, str) else content)


def _stage(scratch: Path, caddyfile_text: str, conf_dir: Path, changes: Mapping[str, str | None]) -> Path:
    """Copy the configuration as it will be after ``changes`` into ``scratch``; return its Caddyfile.

    The copy imports its own conf.d, which holds the changed blocks and every other ``*.caddy``
    entry Caddy imports today, with its mode and owner. An entry that is not a regular file
    stops the change: Caddy would follow a link anywhere, and a copy that the caddy user can
    read could disclose what it points to.
    """
    user = _caddy_user()
    staged_conf = scratch / "conf.d"
    staged_conf.mkdir()
    live_import, staged_import = _import_line(conf_dir), _import_line(staged_conf)
    lines = caddyfile_text.split("\n")
    if lines.count(live_import) != 1:
        raise CaddyError(f"the Caddyfile does not import {conf_dir}/*{_SUFFIX} exactly once; "
                         "run gateway/tools/setup-caddy.sh again")
    staged = scratch / "Caddyfile"
    _write_new(staged, "\n".join(staged_import if line == live_import else line for line in lines), 0o644)
    replaced = {f"{name}{_SUFFIX}" for name in changes}
    for path in sorted(conf_dir.iterdir()):
        if not path.name.endswith(_SUFFIX) or path.name in replaced:
            continue
        try:
            data, st = _read_regular(path)
        except OSError as exc:
            raise CaddyError(f"Caddy imports {path}, which is not a regular file ({exc.strerror}). Move it out "
                             f"of {conf_dir} (gateway/tools/setup-caddy.sh does), then run this again") from None
        owner = (st.st_uid, st.st_gid) if user is not None else None
        _write_new(staged_conf / path.name, data, stat.S_IMODE(st.st_mode), owner)
    for name, content in changes.items():
        if content is not None:
            _write_new(staged_conf / f"{name}{_SUFFIX}", content, 0o644)
    if user is not None:  # caddy validates the copy: let it in, read-only
        for directory in (scratch, staged_conf):
            os.chown(directory, 0, user[1])
            os.chmod(directory, 0o750)
    return staged


def _not_running_hint(output: str) -> str:
    lowered = output.lower()
    if "admin.sock" in lowered and ("no such file" in lowered or "connection refused" in lowered):
        return ("\nCaddy is not running. Start it (sudo systemctl start caddy-webspec.socket "
                "caddy-webspec.service), then run this again.")
    if _TIMED_OUT in output:
        return ("\nCaddy may still have loaded the change after all: have it load the files on disk "
                "(sudo systemctl reload caddy-webspec), then run this again.")
    return ""


def apply_site_changes(
    changes: Mapping[str, str | None],
    *,
    domain: str | None = None,
    caddyfile: Path | None = None,
    conf_dir: Path | None = None,
) -> bool:
    """Make ``changes`` (site block name -> its new text, or None to remove it) as one transaction.

    ``domain``, if given, is the public domain the blocks serve, which the Caddyfile's header
    then records. The change is staged and validated with the trusted caddy first, then written
    and loaded with ``caddy reload``. Returns False if nothing had to change. Raises CaddyError,
    with every file as it was, if this host's Caddy is not :data:`MANAGED` (:func:`caddy_status`),
    if Caddy rejects the configuration, or if it does not load it: the running Caddy then keeps
    its previous configuration, which is again the one on disk. So a rejected change never
    leaves a configuration that cannot start.
    """
    cf = caddyfile or CADDYFILE
    d = conf_dir or CADDY_CONF_DIR
    status, why = caddy_status(cf, d)
    if status != MANAGED:
        raise CaddyError(why)
    binary = caddy_binary()
    with _locked(d):
        info = read_caddyfile(cf)
        if info is None or not (info.trusted and info.generated):
            raise CaddyError(f"{cf} changed while webspec-ctl was running; run this again")
        try:
            caddyfile_text = info.text if domain is None else _with_recorded_domain(info, domain)
        except ValueError as exc:
            raise CaddyError(str(exc)) from None
        todo: dict[str, str | None] = {}
        for name, content in changes.items():
            path = d / f"{_check_name(name)}{_SUFFIX}"
            if content is None and not os.path.lexists(path):
                continue
            if content is not None and _current_text(path) == content:
                continue
            todo[name] = content
        if not todo and caddyfile_text == info.text:
            return False
        snapshots = [_snapshot(d / f"{name}{_SUFFIX}") for name in todo]
        if caddyfile_text != info.text:
            snapshots.append(_snapshot(cf))
        with _scratch_dir() as tmp:
            scratch = Path(tmp)
            staged = _stage(scratch, caddyfile_text, d, todo)
            ok, output = _run_caddy(binary, ["validate", "--config", str(staged)], scratch)
            if not ok:
                raise CaddyError(f"Caddy rejects the new configuration, so nothing was changed:\n{output}")
            try:
                for name, content in todo.items():
                    if content is None:
                        remove_site_block(name, conf_dir=d)
                    else:
                        write_site_block(name, content, conf_dir=d)
                if caddyfile_text != info.text:
                    _atomic_write(cf, caddyfile_text, info.mode)
                ok, output = _run_caddy(binary, ["reload", "--config", str(cf)], scratch)
            except BaseException:
                # Interrupted (Ctrl-C) or failed half-way: the files Caddy last loaded go back.
                _restore(snapshots)
                raise
            if not ok:
                failed = _restore(snapshots)
                if failed:
                    raise CaddyError(
                        f"Caddy did not load the change:\n{output}\nThese files could not be put back, so "
                        f"the configuration on disk may not start: {'; '.join(failed)}. Repair it with "
                        "gateway/tools/setup-caddy.sh.")
                raise CaddyError(f"Caddy did not load the change, so every file was put back as it was:\n"
                                 f"{output}{_not_running_hint(output)}")
    return True


# ── command line for gateway/tools/setup-caddy.sh ──

# Exit status of plan, stage and apply when the new configuration would stop serving hosts that
# Caddy serves today and --allow-shrink (ALLOW_SHRINK=1) was not given: nothing was changed.
SHRINK_REFUSED = 3


def ingress_rule(name: str, domain: str) -> list[str]:
    """The cloudflared ingress rule that sends a direct site's public host to the direct listener."""
    return [f"  - hostname: {_check_name(name)}.{_check_domain(domain)}",
            f"    service: http://127.0.0.1:{DIRECT_PORT}"]


def ingress_rules(domain: str, sites: Iterable[Site] | None = None, conf_dir: Path | None = None) -> list[str]:
    """cloudflared's ingress rules for this configuration, as YAML lines; [] without a public domain.

    Each direct site's public host goes to the direct listener, ahead of the domain's wildcard,
    which goes to the gateway's listener, the only one the agent's allow-list names (DP-3).
    ``sites`` defaults to the trusted blocks in conf.d.
    """
    if not domain:
        return []
    chosen = scan_conf_dir(conf_dir)[0].values() if sites is None else sites
    lines = ["ingress:"]
    for site in sorted((s for s in chosen if s.kind == "direct"), key=lambda s: s.name):
        lines += ingress_rule(site.name, domain)
    return lines + [f'  - hostname: "*.{_check_domain(domain)}"', f"    service: http://127.0.0.1:{CADDY_PORT}",
                    "  - service: http_status:404"]


def _load_services(path: Path) -> dict[str, Any]:
    """The gateway's services. Raises FileNotFoundError if the config does not exist, or what parsing raises."""
    from .config import parse_claude_config

    if not path.exists():
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
    return parse_claude_config(path)


def _write_config(
    plan: SitePlan,
    domain: str,
    *,
    caddyfile: Path,
    conf_dir: Path,
    log_dir: Path | None,
    gateway_port: int,
    caddy_port: int,
    disabled_dir: Path | None,
    listeners: Listeners,
    remove_stale: bool,
) -> None:
    """Write a Caddyfile that imports ``conf_dir`` and the planned site blocks into it."""
    texts = {name: render_site(site, domain, gateway_port=gateway_port, caddy_port=caddy_port,
                               listen=listeners, log_dir=log_dir)
             for name, site in plan.sites.items()}
    caddyfile_text = generate_global_caddyfile(caddy_port=caddy_port, conf_dir=conf_dir, listen=listeners,
                                               domain=domain)
    if disabled_dir is not None and plan.untrusted:
        # Root only: the files may be anything an earlier, agent-writable setup left behind.
        for directory in (disabled_dir.parent, disabled_dir):
            if directory.is_symlink():
                raise ValueError(f"{directory} is a symlink; refusing to move files into it")
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        for path in plan.untrusted:
            os.rename(path, disabled_dir / path.name)  # moves a symlink itself, never its target
    conf_dir.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        write_site_block(name, text, conf_dir=conf_dir)
    if remove_stale:
        for name in plan.stale:
            remove_site_block(name, conf_dir=conf_dir)
    _atomic_write(caddyfile, caddyfile_text)


def _print_sites(plan: SitePlan, domain: str) -> None:
    for name, site in sorted(plan.sites.items()):
        print(f"  {name + _SUFFIX:<24} {site.describe(domain)}")
    for name in plan.stale:
        print(f"  {name + _SUFFIX:<24} removed: no longer in the gateway config")
    for note in plan.notes:
        print(f"  note: {note}")


def _print_hosts(report: HostReport, allow_shrink: bool) -> bool:
    """Say what becomes of the hosts Caddy serves today; False if some may be dropped unasked (F9)."""
    # Read from the files on disk: after a run that stopped once they were written, Caddy may
    # still run the configuration before them.
    print(f"  Hosts the configuration on disk serves: {len(report.before)}; still served afterwards: "
          f"{len(report.before) - len(report.dropped)}.")
    if report.moved:
        print(f"  Moved to the direct listener, 127.0.0.1:{DIRECT_PORT} (DP-3): {', '.join(report.moved)}.")
        print("  cloudflared must send their public hosts there: see the ingress rules at the end.")
    if report.left_direct:
        print(f"  No longer direct sites, now served through the gateway on 127.0.0.1:{CADDY_PORT}: "
              f"{', '.join(report.left_direct)}.")
        print(f"  cloudflared must stop sending their public hosts to :{DIRECT_PORT}: remove the ingress rules "
              "that do, as the rules at the end show.")
    for entry in report.unreadable:
        print(f"  Cannot tell which hosts this serves, and it is not kept: {entry}.")
    if not report.refused(allow_shrink):
        if report.dropped:
            print(f"  No longer served, as ALLOW_SHRINK=1 allows: {', '.join(report.dropped)}.")
        return True
    sys.stdout.flush()  # the plan above comes first in a log that holds both streams
    if report.dropped:
        print("webspec.caddy: these hosts are served today, and would not be served afterwards:", file=sys.stderr)
        for host in report.dropped:
            print(f"  {host}", file=sys.stderr)
    if report.unreadable:
        print("webspec.caddy: the hosts of the files above cannot all be read, so the new configuration may "
              "stop serving some of them:", file=sys.stderr)
        for entry in report.unreadable:
            print(f"  {entry}", file=sys.stderr)
    return False


def _write_hosts(report: HostReport, path: Path) -> None:
    """``host TAB port TAB kept|moved|left-direct|dropped TAB before`` for every host Caddy serves today.

    setup-caddy.sh probes each host at ``before``, the port it is served on today ("-" if no
    address says), before it changes anything, and at ``port`` once it has: where the host is
    served afterwards, or, for a dropped one, where it should now get the catch-all's 421.
    "moved": to the direct listener; "left-direct": from it to the gateway's.
    """
    lines = []
    moved, left = set(report.moved), set(report.left_direct)
    for host in sorted(report.before):
        before = min((p for p in report.before[host] if p), default=None)
        if host in report.after:
            state = "moved" if host in moved else "left-direct" if host in left else "kept"
            port = report.after[host]
        else:
            state, port = "dropped", before or CADDY_PORT
        lines.append(f"{host}\t{port}\t{state}\t{before or '-'}\n")
    path.write_text("".join(lines), encoding="utf-8")


def _print_moved(plan: SitePlan, disabled_dir: Path) -> None:
    if not plan.untrusted:
        return
    print(f"  {len(plan.untrusted)} file(s) not written by webspec.caddy, or writable by others, "
          f"moved to {disabled_dir}:")
    for path in plan.untrusted:
        name = path.name[: -len(_SUFFIX)]
        if name in plan.adopted:
            note = f"regenerated as a direct site, on the direct listener (:{DIRECT_PORT})"
        elif name in plan.sites:
            note = "replaced by the generated block"
        else:
            note = "no longer served"
        print(f"    {path.name!r}: {note}")  # repr: a file name may hold terminal escapes


def main(argv: Sequence[str] | None = None) -> int:
    """Command line for gateway/tools/setup-caddy.sh (``python -I -m webspec.caddy``)."""
    parser = argparse.ArgumentParser(prog="python -m webspec.caddy",
                                     description="Write the WebSpec Caddy configuration (setup-caddy.sh).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("domain", help="print the public domain (resolve_domain) and where it came from, "
                                  "tab-separated")
    sub.add_parser("listeners", help="print the socket layout to install: dual, or ipv4 on a kernel without IPv6")
    ingress = sub.add_parser("ingress", help="print cloudflared's ingress rules for the live configuration")
    ingress.add_argument("--domain", required=True, help="the public domain; empty for none")
    for name, text in (("plan", "say what the new configuration serves, and refuse to drop hosts; change nothing"),
                       ("stage", "write the configuration into DIR, for validation"),
                       ("apply", "write the live configuration")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--config", type=Path, required=True, help="the gateway config (mcpServers)")
        p.add_argument("--domain", required=True, help="the public domain; empty for loopback names only")
        p.add_argument("--listeners", choices=sorted(LAYOUTS), required=True,
                       help="the layout of the caddy-webspec.socket that setup-caddy.sh installs")
        p.add_argument("--gateway-port", type=int, default=7002)
        p.add_argument("--caddy-port", type=int, default=CADDY_PORT)
        p.add_argument("--allow-shrink", action="store_true",
                       help="stop serving hosts that Caddy serves today (ALLOW_SHRINK=1)")
        if name == "plan":
            p.add_argument("--hosts-out", type=Path, help="write the hosts served today, and what becomes of them")
        elif name == "stage":
            p.add_argument("--out", type=Path, required=True, help="the staging directory")
        else:
            p.add_argument("--disabled", type=Path, required=True,
                           help="where files this module did not write are moved")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ValueError as exc:
        print(f"webspec.caddy: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if args.command == "domain":
        # The one webspec-ctl would use: a re-run keeps the domain the configuration records.
        domain = resolve_domain()
        print(f"{domain.name}\t{domain.source}" + (f"; {domain.note}" if domain.note else ""))
        return 0
    if args.command == "listeners":
        print(planned_listeners().name)
        return 0
    if args.domain:
        _check_domain(args.domain)
    if args.command == "ingress":
        print("\n".join(ingress_rules(args.domain)))
        return 0

    listeners = LAYOUTS[args.listeners]
    try:
        services = _load_services(args.config)
    except FileNotFoundError:
        # Never "no services": that would drop every service block (F9).
        print(f"webspec.caddy: the gateway config {args.config} does not exist; nothing was changed",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - whatever the parse failure, change nothing
        print(f"webspec.caddy: cannot parse {args.config}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    plan = plan_sites(services, CADDY_CONF_DIR, gateway_port=args.gateway_port, adopt_legacy=True)
    report = host_report(plan, args.domain, caddy_port=args.caddy_port)
    if args.command == "plan":
        if not services:
            print(f"  {args.config} lists no services: no service block is added, the site blocks "
                  "webspec.caddy wrote are kept, and the others are moved out of the import glob")
        _print_sites(plan, args.domain)
        ok = _print_hosts(report, args.allow_shrink)
        if args.hosts_out is not None:
            _write_hosts(report, args.hosts_out)
        return 0 if ok else SHRINK_REFUSED
    if report.refused(args.allow_shrink):
        lost = ", ".join(report.dropped) or f"the hosts of {'; '.join(report.unreadable)}"
        print(f"webspec.caddy: the new configuration may stop serving {lost}; nothing was changed "
              "(--allow-shrink allows it)", file=sys.stderr)
        return SHRINK_REFUSED

    common = {"gateway_port": args.gateway_port, "caddy_port": args.caddy_port, "listeners": listeners}
    if args.command == "stage":
        out = args.out
        _write_config(plan, args.domain, caddyfile=out / "Caddyfile", conf_dir=out / "conf.d", log_dir=out / "log",
                      disabled_dir=None, remove_stale=False, **common)
        print(f"  staged {len(plan.sites)} site block(s) in {out}")
        return 0
    _write_config(plan, args.domain, caddyfile=CADDYFILE, conf_dir=CADDY_CONF_DIR, log_dir=None,
                  disabled_dir=args.disabled, remove_stale=True, **common)
    print(f"  wrote {len(plan.sites)} site block(s)" + (f", removed {len(plan.stale)}" if plan.stale else ""))
    _print_moved(plan, args.disabled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
