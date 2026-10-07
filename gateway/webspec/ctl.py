"""webspec-ctl: provision and manage WebSpec services.

Usage:
    webspec-ctl add <name> [options]
    webspec-ctl ls
    webspec-ctl health [name]
    webspec-ctl rm <name> [--clean-env KEY ...]
    webspec-ctl caddy-sync [--no-public]
    webspec-ctl approve [challenge.json] --key KEY

The gateway config is WEBSPEC_CONFIG; else /etc/webspec/config.json on a production host (one
where that file exists); else ~/.claude.json. Every command prints the one it uses.

add, rm and caddy-sync also change the Caddy site blocks, on a host that
gateway/tools/setup-caddy.sh has set up, as one transaction (webspec.caddy.apply_site_changes):
Caddy validates and loads the change, or nothing changes. Run them as root there:
    sudo /opt/webspec/venv/bin/webspec-ctl caddy-sync
On macOS the deployment ships no proxy, and they leave Caddy out.

A service is served on Caddy's gateway listener, 127.0.0.1:7001 and [::1]:7001, the only one the
agent's egress allow-list names. A direct site (add --direct) bypasses the gateway, so it is
served on the direct listener, 127.0.0.1:7003 and [::1]:7003, which the allow-list must leave
out (DP-3); cloudflared sends its public host there with an ingress rule of its own.
"""

from __future__ import annotations

import argparse
import http.client
import os
import sys
import time
import urllib.parse
from pathlib import Path

from .approve_cli import add_parser as add_approve_parser, cmd_approve
from .caddy import (
    ABSENT,
    DIRECT_PORT,
    MANAGED,
    REFUSED,
    SAFE_PATH,
    CaddyError,
    Domain,
    Listeners,
    apply_site_changes,
    caddy_status,
    check_direct_upstream,
    gateway_domain_hint,
    generate_direct_site_block,
    generate_site_block,
    ingress_rule,
    ingress_rules,
    installed_listeners,
    listener_mismatch,
    live_site,
    plan_sync,
    recorded_domain,
    resolve_domain,
    site_block_exists,
    unheld_listeners,
    upstream_address,
)
from .config import ServiceRegistry, normalize_name, parse_claude_config
from .methods import LEVEL_NAMES, parse_level
from .config_writer import (
    add_env_var,
    add_service,
    check_env_name,
    default_config_path,
    default_env_path,
    ignored_export_warning,
    list_services,
    remove_env_var,
    remove_service,
)

CADDY_PORT = int(os.environ.get("WEBSPEC_PORT", "7001"))
GATEWAY_PORT = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7002"))
# Dial loopback by IP: a name could resolve to ::1 first, where any local user could listen.
LOOPBACK = "127.0.0.1"
SUDO_CTL = "sudo /opt/webspec/venv/bin/webspec-ctl"


def _probe(name: str, port: int, timeout: float) -> int | None:
    """The status of ``HEAD /`` for ``name.localhost`` at 127.0.0.1:``port``; None if nothing answers.

    The connection goes to the IP and the Host header is set explicitly: ``*.localhost`` may not
    resolve at all (stock Debian), or may resolve to ::1 first, where any local user could listen
    and answer root's probe.
    """
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        conn.request("HEAD", "/", headers={"Host": f"{name}.localhost:{port}"})
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


# What a probe of a service finds (_health_check): it answers, or it answers as a guarded one.
OK, GUARDED = "ok", "guarded"


def _answer(status: int | None) -> str | None:
    """OK, GUARDED, or None (down) for the status of a probe.

    GUARDED: 401 or 403, which the gateway answers to every request without a guard to a
    guarded service (level 1 and up), HEAD / included (DS-2): the name is served, and the
    server behind it is not checked without a signed request. None: no answer, or another
    error: 421 from Caddy's catch-all (no site block), 404 from the gateway (no such service),
    429, a 5xx.
    """
    if status is None:
        return None
    if status < 400:
        return OK
    return GUARDED if status in (401, 403) else None


def _health_check(name: str, port: int = CADDY_PORT, timeout: float = 5.0) -> str | None:
    """HEAD / for the service's subdomain at 127.0.0.1:``port``: OK, GUARDED, or None (:func:`_answer`)."""
    return _answer(_probe(name, port, timeout))


def _first_answer(name: str, ports: tuple[int, ...], timeout: float) -> str | None:
    """What the first of ``ports`` that serves ``name`` answers (:func:`_health_check`); None if none does."""
    for port in ports:
        found = _health_check(name, port=port, timeout=timeout)
        if found is not None:
            return found
    return None


def _health_text(found: str | None, down: str, ok: str = "ok") -> str:
    """``ok``, ``ok`` followed by "(guarded)", or ``down``: what the commands print for a probe."""
    return {OK: ok, GUARDED: f"{ok} (guarded)"}.get(found, down)


_GUARDED_NOTE = ("(guarded): the gateway serves the name, and answers 401 to a request without its guard, as it "
                 "must. Only a signed request reaches the MCP server behind it, which this does not check.")


def _wait_for_gateway(name: str, port: int = GATEWAY_PORT, max_wait: float = 35.0,
                      also: tuple[int, ...] = ()) -> bool:
    """Poll gateway until service appears (config reload cycle), at ``port`` or one of ``also``."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        for candidate in (port, *also):
            # 401/403 means service exists but requires auth — that's fine
            if _answer(_probe(name, candidate, 3.0)) is not None:
                return True
        time.sleep(2)
    return False


def _other_gateway_ports(status: str) -> tuple[int, ...]:
    """Where else the gateway may listen, besides GATEWAY_PORT, for probes.

    Behind Caddy, and on every production host (contract C1), the gateway listens on
    WEBSPEC_INTERNAL_PORT (7002), and Caddy's port is Caddy's. Where there is no Caddy, a
    development gateway started without WEBSPEC_INTERNAL_PORT (the macOS agent, gateway/launchd/)
    listens on WEBSPEC_PORT, 7001, as `python -m webspec` does.
    """
    return (CADDY_PORT,) if status == ABSENT and CADDY_PORT != GATEWAY_PORT else ()


def _url(name: str, public: bool, domain: str, https: bool = False, port: int | None = None) -> str:
    """The URL to show: the public one for a site served on the public domain, else the loopback one.

    ``port`` is the Caddy listener of the loopback one: the gateway's, unless it is a direct site.
    """
    if public and domain:
        return f"{'https' if https else 'http'}://{name}.{domain}"
    note = " (no public domain is configured)" if public else ""
    return f"http://{name}.localhost:{CADDY_PORT if port is None else port}{note}"


def _direct_site_notice(name: str, domain: str, listeners: Listeners) -> list[str]:
    """What the operator must know about a direct site: DP-3, and the cloudflared rule it needs."""
    lines = [
        f"DP-3: {name} bypasses the gateway (no guard, no audit log), so it is served on the direct",
        f"listener, {listeners.where('direct')}. Keep that port out of the agent's egress allow-list,",
        f"which names the gateway's listener only: {listeners.where('service')}.",
    ]
    if not domain:
        return lines + [f"No public domain: {name} is served to local processes only, as "
                        f"http://{name}.localhost:{DIRECT_PORT}."]
    return lines + [
        f"cloudflared: send {name}.{domain} to the direct listener with this ingress rule, placed above",
        f"the rule for *.{domain}, then restart cloudflared (keep the Host header: no httpHostHeader):",
        *ingress_rule(name, domain),
    ]


def _tunnel_rule_reminder(name: str, hosts: list[str]) -> str:
    """A direct site is gone: the cloudflared rule that sent its public host to the direct listener must go too."""
    rules = "; ".join(f"hostname: {host}" for host in hosts)
    return (f"{name} was a direct site: remove its cloudflared ingress rule to :{DIRECT_PORT} ({rules}) too, then "
            "restart cloudflared")


def _error(message: str) -> int:
    sys.stdout.flush()  # what was done so far comes first in a log that holds both streams
    print(f"Error: {message}", file=sys.stderr)
    return 1


def _warn(message: str) -> None:
    sys.stdout.flush()  # in order, as for _error
    print(f"Warning: {message}", file=sys.stderr)


def _load_entries(path: Path) -> dict | None:
    """The raw mcpServers of the gateway config; None, with the error printed, if it cannot be read."""
    try:
        return list_services(path)
    except FileNotFoundError:
        _error(f"the gateway config {path} does not exist. On a production host run webspec-ctl as "
               f"root ({SUDO_CTL} …); elsewhere set WEBSPEC_CONFIG.")
    except PermissionError:
        _error(f"cannot read the gateway config {path}: permission denied. Run webspec-ctl as root "
               f"({SUDO_CTL} …), or set WEBSPEC_CONFIG.")
    except (OSError, ValueError) as exc:
        _error(f"cannot read the gateway config {path} ({type(exc).__name__}: {exc})")
    return None


def _domain(args: argparse.Namespace | None = None) -> Domain:
    """The public domain the site blocks serve: --no-public, else caddy.resolve_domain().

    Raises ValueError (DomainError) when it is ambiguous or malformed.
    """
    if args is not None and getattr(args, "no_public", False):
        return Domain("", "--no-public", explicit=True)
    return resolve_domain()


def _shown_domain() -> str:
    """The public domain for display only; none when it cannot be determined."""
    try:
        return resolve_domain().name
    except ValueError:
        return ""


def _describe_domain(domain: Domain) -> str:
    return f"{domain.name or '(none: *.localhost names only)'} ({domain.source})"


def _domain_mismatch(domain: Domain) -> str | None:
    """Why one new site block must not use ``domain``: the configuration serves another one.

    add writes a single block, and a public domain is the configuration's: changing it would
    leave the other blocks on the old one. caddy-sync changes it for every block at once, but
    only when told to: a bare caddy-sync keeps the recorded domain.
    """
    recorded, where = recorded_domain()
    if recorded is None or domain.name == recorded:
        return None
    given = "WEBSPEC_DOMAIN" if domain.explicit else domain.source
    if domain.name:
        change = f"sudo WEBSPEC_DOMAIN={domain.name} {SUDO_CTL.removeprefix('sudo ')} caddy-sync"
    else:
        change = f"{SUDO_CTL} caddy-sync --no-public"
    return (f"the Caddy configuration serves {recorded or 'no public domain'} ({where}), but {given} says "
            f"{domain.name or 'none'}. add writes one site block; change the domain of every block first "
            f"with `{change}`, then run this again.")


def _print_note(domain: Domain) -> None:
    if domain.note:
        print(f"Note:    {domain.note}")


def _direct_upstream(args: argparse.Namespace) -> tuple[str, int]:
    """Return (host, port) of a direct site's app. Only an app on a loopback address is accepted.

    ``localhost`` (and a bare --port) means 127.0.0.1: an app that listens on ::1 only must
    be named as http://[::1]:PORT. Caddy's listeners and the gateway's port are refused, as for
    the direct sites of an earlier setup (P12): a direct site to Caddy loops through it.
    """
    if not args.url:
        host, port = LOOPBACK, args.port
    else:
        parts = urllib.parse.urlsplit(args.url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"--url {args.url!r} must be http://HOST:PORT (Caddy proxies plain HTTP on loopback)")
        try:
            port = parts.port
        except ValueError:
            raise ValueError(f"--url {args.url!r} has an invalid port") from None
        if port is None:
            raise ValueError(f"--url {args.url!r} must include the port")
        if args.port and args.port != port:
            raise ValueError(f"--port {args.port} and --url {args.url!r} disagree")
        host = LOOPBACK if parts.hostname == "localhost" else parts.hostname
    check_direct_upstream(host, port, gateway_port=GATEWAY_PORT)  # raises unless an app on loopback
    return host, port


def _add_direct(name: str, args: argparse.Namespace) -> int:
    """A Caddy-only proxy to a local web app, on the direct listener (DP-3).

    The gateway and its config are not involved.
    """
    if not args.port and not args.url:
        print("Error: --direct requires --port or --url", file=sys.stderr)
        return 1
    # A direct site must not shadow a gateway service. Read the config caddy-sync reads, so
    # that this also works through sudo on a production host.
    path = default_config_path()
    print(f"Config:  {path}")
    status, why = caddy_status()
    if status != MANAGED:
        return _error(f"a direct site is a Caddy site block, and {why}.")
    try:
        services = _gateway_services(path)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: cannot read the gateway config {path} ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    if name in services:
        print(f"Error: '{name}' is a gateway service in {path}; a direct site would bypass its guard. "
              "Choose another name.", file=sys.stderr)
        return 1
    try:
        listeners = installed_listeners()  # what caddy-webspec.socket passes, never a guess (F26)
        domain = _domain()
        mismatch = _domain_mismatch(domain)
        if mismatch:
            return _error(mismatch)
        _print_note(domain)
        target_host, target_port = _direct_upstream(args)
        content = generate_direct_site_block(
            name=name, domain=domain.name, target_port=target_port, target_host=target_host,
            listen=listeners.direct,
        )
    except (ValueError, CaddyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        changed = apply_site_changes({name: content}, domain=domain.name)
    except CaddyError as exc:
        return _error(str(exc))
    print(f"Generated direct Caddy proxy for {name} → {upstream_address(target_host, target_port)}")
    print("Caddy reloaded" if changed else "Caddy:   the site block was already current")

    # Quick health check, on the direct listener. 401 or 403 come from the app, which answers.
    status = "ok" if _health_check(name, port=DIRECT_PORT) else "unreachable"

    print()
    print(f"  Name:    {name}")
    print("  Type:    direct (bypasses the gateway)")
    print("  Guard:   no")
    print(f"  Listen:  {listeners.where('direct')} (the direct listener)")
    print(f"  Health:  {status}")
    print(f"  URL:     {_url(name, True, domain.name, args.https, port=DIRECT_PORT)}")
    print()
    for line in _direct_site_notice(name, domain.name, listeners):
        print(line)
    return 0


def _resolve_guard(args: argparse.Namespace, previous: dict) -> bool:
    """The guard flag the entry gets, before its level is applied.

    An explicit flag wins; an explicit --level 0 means no guard; --port is a local proxy,
    unguarded unless asked; --force keeps the existing setting; new services are guarded.
    """
    if args.level == 0:
        return False
    if args.guard is not None:
        return args.guard
    if args.port:
        return False
    if previous:
        return bool(previous.get("guard", False))
    return True


def cmd_add(args: argparse.Namespace) -> int:
    """Provision a new service."""
    raw_name = args.name
    name = normalize_name(raw_name)
    if not name:
        print(f"Error: invalid service name '{raw_name}'", file=sys.stderr)
        return 1

    # --direct: Caddy-only proxy, bypass gateway entirely
    if args.direct:
        return _add_direct(name, args)

    path = default_config_path()
    print(f"Config:  {path}")
    if args.level == 0 and args.guard:
        return _error("--level 0 is the unguarded level, so it takes no --guard (every level from 1 up "
                      "is guarded)")
    for secret in args.secret or []:
        try:
            check_env_name(secret)
        except ValueError as exc:
            return _error(f"--secret: {exc}")

    # Check for duplicates
    existing = _load_entries(path)
    if existing is None:
        return 1
    if name in existing and not args.force:
        print(f"Error: service '{name}' already exists (use --force to update)", file=sys.stderr)
        return 1
    previous = existing.get(name)
    previous = previous if isinstance(previous, dict) else {}

    # --command alone means a stdio server; --url or --port an http one.
    svc_type = args.svc_type or ("stdio" if args.svc_command and not (args.url or args.port) else "http")
    url = args.url
    # --port shorthand: expand to --type http --url http://127.0.0.1:PORT (unguarded unless asked)
    if args.port:
        svc_type = "http"
        url = url or f"http://{LOOPBACK}:{args.port}"

    # Build service config
    entry: dict = {}

    if svc_type == "http":
        if not url:
            print("Error: --url required for http services (or --command for a stdio one)", file=sys.stderr)
            return 1
        entry["type"] = "http"
        entry["url"] = url
        if args.header:
            headers = {}
            for h in args.header:
                if ":" not in h:
                    print(f"Error: invalid header format '{h}' (expected KEY:VALUE)", file=sys.stderr)
                    return 1
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
            entry["headers"] = headers
    else:
        if not args.svc_command:
            print("Error: --command required for stdio services", file=sys.stderr)
            return 1
        entry["type"] = "stdio"
        entry["command"] = args.svc_command
        if args.svc_args:
            entry["args"] = args.svc_args
        if args.svc_env:
            env = {}
            for e in args.svc_env:
                if "=" not in e:
                    print(f"Error: invalid env format '{e}' (expected KEY=VALUE)", file=sys.stderr)
                    return 1
                k, v = e.split("=", 1)
                env[k] = v
            entry["env"] = env

    if args.namespace:
        entry["namespace"] = args.namespace

    # Security settings are never silently dropped by --force: keep the existing
    # level / tool overrides / qualifier labels unless explicitly replaced.
    for key in ("level", "tools", "labels"):
        if key in previous:
            entry[key] = previous[key]
    if args.level is not None:
        entry["level"] = args.level

    # One source of truth, as the gateway reads it: guarded exactly when the level is 1 or more.
    # The printed Guard, the URL and the site block (public host or not) all follow it.
    level = parse_level(entry.get("level"), guard=_resolve_guard(args, previous), service=name)
    guarded = level >= 1
    if args.guard is False and guarded:
        return _error(f"--no-guard, but '{name}' is at level {level} ({LEVEL_NAMES[level]}), and every level "
                      "from 1 up is guarded. Pass --level 0 to serve it unguarded, on loopback names only.")
    if guarded:
        entry["guard"] = True

    # Caddy first, as far as it can be checked without changing anything: a host that cannot take
    # the site block gets no config entry either.
    status, why = caddy_status()
    if status == REFUSED:
        return _error(f"{why}.\nNothing was changed.")
    content = None
    replaced = None
    if status == MANAGED:
        try:
            # The service's block would replace a direct site of that name, and so the app's
            # proxy: only when asked, as add --direct refuses a gateway service's name (P10).
            site = live_site(name)
            replaced = site if site is not None and site.kind == "direct" else None
            if replaced is not None and not args.force:
                return _error(f"'{name}' is a direct site in Caddy (-> {replaced.upstream}); a gateway service of "
                              "that name would replace it. Choose another name, or pass --force to replace it.\n"
                              "Nothing was changed.")
            domain = _domain()
            mismatch = _domain_mismatch(domain)
            if mismatch:
                return _error(mismatch)
            _print_note(domain)
            content = generate_site_block(
                name=name, domain=domain.name,
                gateway_port=GATEWAY_PORT, guard=guarded,
                caddy_port=CADDY_PORT,
                # The gateway's listener as caddy-webspec.socket passes it, never a guess (F26).
                listen=installed_listeners().gateway,
            )
        except (ValueError, CaddyError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        domain_name = domain.name
    else:
        domain_name = _shown_domain()

    # Write config
    add_service(name, entry, path=path)
    print(f"Added service '{name}' to {path}")

    if content is not None:
        try:
            changed = apply_site_changes({name: content}, domain=domain_name)
        except BaseException as exc:
            # Caddy kept its previous configuration: put the gateway config back as well, so that
            # the command changed nothing at all.
            if name in existing:
                add_service(name, existing[name], path=path)
            else:
                remove_service(name, path=path)
            if not isinstance(exc, CaddyError):
                raise
            return _error(f"{exc}\n{path} was put back as it was: nothing was changed.")
        print(f"Generated Caddy config for {name}" if changed else f"Caddy config for {name} was already current")
        if changed:
            print("Caddy reloaded")
        if replaced is not None:
            print(f"The service's site block replaced the direct site {name} (-> {replaced.upstream}), as --force "
                  "allows")
            if domain_name:
                print(_tunnel_rule_reminder(name, [f"{name}.{domain_name}"]))
    else:
        print(f"Caddy:   skipped: {why}")

    # Add secret placeholders. None after an `export S=` line, which sets S where a shell sources the
    # file; where systemd reads it instead, the warning says how to fix that line in place (P11).
    if args.secret:
        env_path = default_env_path()
        for s in args.secret:
            ignored = ignored_export_warning(s, env_path)
            if add_env_var(s, path=env_path):
                print(f"Added placeholder {s} to {env_path}")
            elif ignored is None:
                print(f"{s} is already set in {env_path}")
            if ignored:
                _warn(f"{ignored}.")

    # Wait for gateway config reload
    print("Waiting for gateway to pick up config...", end=" ", flush=True)
    others = _other_gateway_ports(status)
    ready = _wait_for_gateway(name, port=GATEWAY_PORT, also=others)
    if ready:
        print("ready")
    else:
        print("timeout (gateway may need manual restart)", file=sys.stderr)

    # Health check through Caddy; through the gateway where there is no Caddy
    if status == MANAGED:
        health = _health_text(_health_check(name, port=CADDY_PORT), "unreachable")
    else:
        found = _first_answer(name, (GATEWAY_PORT, *others), 5.0) if ready else None
        health = _health_text(found, "unreachable") + " (gateway; no Caddy here)"

    print()
    print(f"  Name:    {name}")
    print(f"  Type:    {svc_type}")
    print(f"  Level:   {level} {LEVEL_NAMES[level]}")
    print(f"  Guard:   {'yes' if guarded else 'no'}")
    print(f"  Health:  {health}")
    print(f"  URL:     {_url(name, guarded, domain_name, args.https)}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    """List services with status."""
    path = default_config_path()
    print(f"Config:  {path}")
    services = _load_entries(path)
    if services is None:
        return 1
    if not services:
        print("No services configured.")
        return 0
    domain = _shown_domain()
    # Through Caddy where there is one; through the gateway where there is none (macOS).
    status = caddy_status()[0]
    ports = (GATEWAY_PORT, *_other_gateway_ports(status)) if status == ABSENT else (CADDY_PORT,)

    # Header
    print(f"{'NAME':<20} {'TYPE':<8} {'LEVEL':<12} {'HEALTH':<13} {'URL'}")
    print("-" * 82)

    guarded = False
    for name, cfg in sorted(services.items()):
        normalized = normalize_name(name)
        svc_type = cfg.get("type", "stdio")
        level = parse_level(cfg.get("level"), guard=bool(cfg.get("guard")), service=normalized)
        level_text = f"{level} {LEVEL_NAMES[level]}"
        found = _first_answer(normalized, ports, 2.0)
        guarded = guarded or found == GUARDED
        health = _health_text(found, "down")
        # Only guarded services (level 1 and up) are served on the public host.
        url = f"{normalized}.{domain}" if domain and level >= 1 else f"{normalized}.localhost:{CADDY_PORT}"
        print(f"{normalized:<20} {svc_type:<8} {level_text:<12} {health:<13} {url}")
    if guarded:
        print(f"\nok {_GUARDED_NOTE}")

    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Detailed health check for a service (or all)."""
    path = default_config_path()
    print(f"Config:  {path}")
    services = _load_entries(path)
    if services is None:
        return 1
    targets = [args.name] if args.name else list(services.keys())
    # The config the other commands use, not the gateway's own default (~/.claude.json).
    registry = ServiceRegistry(path)
    status = caddy_status()[0]
    # A Caddyfile for other sockets than systemd passes (IPv6 turned on or off at boot since
    # setup, P2): Caddy cannot start, or misses [::1]. Only the journal would say so otherwise.
    # And [::1]:7001 and :7003 that nothing holds, though the kernel has IPv6, where any local
    # process can listen (an earlier setup's IPv4-only drop-in, DP-9): nothing would say so.
    problems = [listener_mismatch() if status == REFUSED else None,
                unheld_listeners() if status != ABSENT else None]
    for problem in filter(None, problems):
        print(f"Caddy:   {problem}")
    guarded = False

    for raw_name in targets:
        name = normalize_name(raw_name)
        print(f"\n--- {name} ---")

        # Caddy proxy check
        if status == ABSENT:
            print("  Caddy proxy:  none on this host")
        else:
            found = _health_check(name, port=CADDY_PORT, timeout=3.0)
            guarded = guarded or found == GUARDED
            print(f"  Caddy proxy:  {_health_text(found, 'unreachable', ok='reachable')}")

        # Gateway direct check
        found = _first_answer(name, (GATEWAY_PORT, *_other_gateway_ports(status)), 3.0)
        guarded = guarded or found == GUARDED
        print(f"  Gateway:      {_health_text(found, 'unreachable', ok='reachable')}")

        # Registry check
        entry = registry.get(name)
        print(f"  Registry:     {'found' if entry else 'not found'}")
        if entry:
            print(f"  Level:        {entry.level} {LEVEL_NAMES[entry.level]}")
            print(f"  Guard:        {'yes' if entry.guard else 'no'}")

    if guarded:
        print(f"\nreachable {_GUARDED_NOTE}")
    return 0


def _remove_site_block(name: str) -> str | None:
    """Drop ``name``'s Caddy site block, printing what was done; why it stays, if it must."""
    status, why = caddy_status()
    if status == ABSENT:
        print(f"Caddy:   skipped: {why}")
        return None
    if not site_block_exists(name):
        print(f"No Caddy site block for {name}")
        return None
    if status == REFUSED:
        return why
    site = live_site(name)
    try:
        apply_site_changes({name: None})
    except CaddyError as exc:
        return str(exc)
    print(f"Removed Caddy config for {name}")
    print("Caddy reloaded")
    domain = _shown_domain()
    if site is not None and site.kind == "direct" and domain:
        print(_tunnel_rule_reminder(name, [f"{name}.{domain}"]))
    return None


def cmd_rm(args: argparse.Namespace) -> int:
    """Deprovision a service.

    The gateway entry goes even when Caddy's site block cannot: it is what revokes the service,
    as the agent reaches the gateway itself (127.0.0.1:7002) whatever Caddy does. A block that
    stays (on a host whose Caddy webspec-ctl may not change, or when Caddy does not load the
    change) only forwards to a gateway that no longer serves the name; rm then exits 1. A name
    the gateway config does not have (a direct site) is Caddy's alone: then nothing changes.
    """
    name = normalize_name(args.name)
    if not name:
        print(f"Error: invalid service name '{args.name}'", file=sys.stderr)
        return 1
    path = default_config_path()
    print(f"Config:  {path}")
    for key in args.clean_env or []:
        try:
            check_env_name(key)
        except ValueError as exc:
            return _error(f"--clean-env: {exc}")

    # A direct site has no entry in the gateway config, and a host may have no such file at all
    # (root's, through sudo): the site block goes regardless.
    try:
        keys = [key for key in list_services(path) if normalize_name(key) == name]
        no_config = False
    except FileNotFoundError:
        keys, no_config = [], True
    except (OSError, ValueError) as exc:
        return _error(f"cannot read the gateway config {path} ({type(exc).__name__}: {exc}). "
                      f"On a production host run webspec-ctl as root ({SUDO_CTL} …).")

    kept = _remove_site_block(name)
    if kept is not None:
        kept = kept.rstrip() + ("" if kept.rstrip().endswith(".") else ".")
    if kept is not None and not keys:
        return _error(f"Caddy's site block for {name} was not removed: {kept}\nNothing was changed.")

    if no_config:
        print(f"No config at {path}; no service entry removed")
    elif not keys:
        print(f"'{name}' is not in {path}; no service entry removed")
    for key in keys:
        if remove_service(key, path=path):
            print(f"Removed '{key}' from {path}")

    # Clean env vars if requested
    if args.clean_env:
        env_path = default_env_path()
        for key in args.clean_env:
            if remove_env_var(key, path=env_path):
                print(f"Removed {key} from {env_path}")
            else:
                print(f"{key} is not set in {env_path}")

    if keys:
        print(f"Service '{name}' deprovisioned. Gateway will drop it on next reload cycle.")
    if kept is not None:
        return _error(f"Caddy's site block for {name} was left in place: {kept}\nThat block only forwards to "
                      f"the gateway, which no longer serves {name} (it answers 404 once it has reloaded its "
                      f"config). Remove the block once Caddy can be changed: {SUDO_CTL} caddy-sync")
    return 0


def _gateway_services(path: Path) -> dict:
    """The gateway's services in ``path``; none if the file does not exist. Raises if it cannot be parsed."""
    try:
        return parse_claude_config(path)
    except FileNotFoundError:
        return {}


def cmd_caddy_sync(args: argparse.Namespace) -> int:
    """Regenerate all Caddy configs from the gateway config.

    A config that is missing or cannot be parsed changes nothing: reading it as empty would
    remove every site block. An empty one removes nothing either (see caddy.plan_sync). The
    public domain stays the one the configuration records, none included, unless WEBSPEC_DOMAIN
    or --no-public says otherwise: a routine sync never drops the public addresses, nor puts
    back the ones --no-public turned off.
    """
    path = default_config_path()
    print(f"Config:  {path}")
    status, why = caddy_status()
    if status != MANAGED:
        return _error(f"{why}.\nNo site block was changed.")
    try:
        services = parse_claude_config(path)
    except Exception as exc:  # noqa: BLE001 - whatever the failure, change nothing
        if not isinstance(exc, OSError):  # read, but not a registry: the file is what to fix
            hint = "Fix the file, then run caddy-sync again. The gateway keeps its last good registry meanwhile."
        elif os.geteuid() != 0:
            hint = "On a production host run this as root; elsewhere, set WEBSPEC_CONFIG."
        else:
            hint = "Check that the file exists and that root can read it, or set WEBSPEC_CONFIG."
        print(f"Error: cannot read the gateway config {path} ({type(exc).__name__}: {exc}).\n"
              f"No site block was changed. {hint}", file=sys.stderr)
        return 1
    try:
        domain = _domain(args)
        recorded, _ = recorded_domain()
    except ValueError as exc:
        return _error(f"{exc}.\nNo site block was changed.")
    print(f"Domain:  {_describe_domain(domain)}")
    domain_changed = recorded is not None and recorded != domain.name
    if domain_changed:
        print(f"         (was {recorded or 'none: *.localhost names only'})")
    _print_note(domain)
    try:
        listeners = installed_listeners()  # what caddy-webspec.socket passes, never a guess (F26)
    except CaddyError as exc:
        return _error(f"{exc}.\nNo site block was changed.")
    result = plan_sync(services, domain.name, gateway_port=GATEWAY_PORT, caddy_port=CADDY_PORT, listen=listeners)
    try:
        changed = apply_site_changes(result.changes, domain=domain.name)
    except CaddyError as exc:
        return _error(str(exc))

    if not services:
        print(f"Warning: {path} lists no services; no site block was removed.", file=sys.stderr)
    for label, names in (("Added", result.added), ("Updated", result.updated), ("Removed", result.removed)):
        if names:
            print(f"{label + ':':<9}{', '.join(names)}")
    for name, hosts in sorted(result.dropped.items()):
        print(f"         {name} no longer serves {', '.join(hosts)}")
    for name, hosts in sorted(result.moved.items()):
        print(f"         {name} moved to the direct listener ({listeners.where('direct')}): {', '.join(hosts)}")
    for name, hosts in sorted(result.left_direct.items()):
        served = [host for host in hosts if host not in result.dropped.get(name, [])]
        if served:
            print(f"         {name} is no longer a direct site: now served through the gateway, on "
                  f"{listeners.where('service')}: {', '.join(served)}")
    if result.unchanged:
        print(f"Unchanged: {', '.join(result.unchanged)}")
    if changed:
        print("Caddy reloaded")
    else:
        print("No change: every site block is current; Caddy was not reloaded.")
    if result.moved and domain.name:
        print(f"\nDP-3: direct sites bypass the gateway and are served on the direct listener, which the agent's "
              f"allow-list leaves out. cloudflared must send their public hosts there; add these ingress rules "
              f"above the rule for *.{domain.name}, then restart cloudflared:")
        for name in sorted(result.moved):
            for line in ingress_rule(name, domain.name):
                print(line)
    # A gateway service replaced these direct sites (P10): the rules that send their public hosts
    # to the direct listener now lead to its catch-all's 421.
    unrouted = {name: [host for host in hosts if not host.endswith(".localhost")]
                for name, hosts in sorted(result.left_direct.items())}
    if any(unrouted.values()):
        print()
        for name, hosts in unrouted.items():
            if hosts:
                print(_tunnel_rule_reminder(name, hosts))
    if domain_changed and changed and domain.name:
        # F31: the gateway reads WEBSPEC_DOMAIN when it starts; until then it answers 404 for the new hosts.
        print(f"\n{gateway_domain_hint(domain.name)}")
        print("cloudflared must route the new domain too; direct sites go to the direct listener (DP-3). Its "
              "ingress rules, in this order:")
        for line in ingress_rules(domain.name, sites=result.plan.sites.values() if result.plan else ()):
            print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webspec-ctl",
        description="Provision and manage WebSpec services",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    # add
    add_p = sub.add_parser("add", help="Provision a new service")
    add_p.add_argument("name", help="Service name (will be normalized to subdomain)")
    add_p.add_argument("--type", choices=["http", "stdio"], dest="svc_type",
                       help="Transport (default: stdio with --command alone, else http)")
    add_p.add_argument("-p", "--port", type=int,
                       help="Shorthand: proxy to 127.0.0.1:PORT (implies --type http; unguarded unless "
                            "--guard or --level says otherwise)")
    add_p.add_argument("--url", help="Remote MCP server URL (required for http); with --direct, "
                                     "the local app as http://127.0.0.1:PORT or http://[::1]:PORT")
    add_p.add_argument("--header", action="append", metavar="KEY:VALUE",
                       help="HTTP header (repeatable, supports ${ENV_VAR})")
    add_p.add_argument("--command", dest="svc_command",
                       help="Command to run (required for stdio)")
    add_p.add_argument("--args", action="append", metavar="ARG", dest="svc_args",
                       help="Command arguments (repeatable)")
    add_p.add_argument("--env", action="append", metavar="KEY=VALUE", dest="svc_env",
                       help="Environment variable (repeatable)")
    add_p.add_argument("--guard", action="store_true", default=None,
                       help="Require HMAC auth (default for new services; --force keeps the existing setting)")
    add_p.add_argument("--no-guard", dest="guard", action="store_false",
                       help="Disable HMAC auth (level 0 only: every level from 1 up is guarded)")
    add_p.add_argument("--secret", action="append", metavar="ENV_VAR",
                       help="Add an empty placeholder for this MCP-server secret to the env file "
                            "(not a WEBSPEC_* setting)")
    add_p.add_argument("--direct", action="store_true",
                       help="Direct Caddy proxy to a local web app on loopback (bypasses the gateway; non-MCP apps)")
    add_p.add_argument("--https", action="store_true",
                       help="Display public URL with https:// (TLS via Cloudflare)")
    add_p.add_argument("--namespace", help="Namespace scheme (phase 2)")
    add_p.add_argument("--level", type=int, choices=range(0, 5),
                       help="Security level 0-4 (docs/spec/levels.md); 0 means no guard. "
                            "With --force, an existing level is kept unless this is given")
    add_p.add_argument("--force", action="store_true",
                       help="Overwrite existing service")

    # ls
    sub.add_parser("ls", help="List services with status")

    # health
    health_p = sub.add_parser("health", help="Detailed health check")
    health_p.add_argument("name", nargs="?", help="Service name (all if omitted)")

    # rm
    rm_p = sub.add_parser("rm", help="Deprovision a service")
    rm_p.add_argument("name", help="Service name to remove")
    rm_p.add_argument("--clean-env", nargs="*", metavar="KEY",
                      help="Remove these MCP-server secrets from the env file")

    # caddy-sync
    sync_p = sub.add_parser("caddy-sync", help="Regenerate all Caddy configs from registry")
    sync_p.add_argument("--no-public", action="store_true",
                        help="Serve loopback names (*.localhost) only, dropping the public domain the "
                             "configuration records (the same as WEBSPEC_DOMAIN=)")

    # approve (level-4 human approval)
    add_approve_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Each line as it is printed, as on a terminal: in a pipe or a log, stdout would be buffered
    # while stderr is not, and an error would come before the lines that led to it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if os.geteuid() == 0:
        # As root, run nothing found through the caller's PATH, which sudo keeps on macOS and a
        # directory on it may be the agent's (DP-1, DP-4). Before anything else runs.
        os.environ["PATH"] = SAFE_PATH
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "add": cmd_add,
        "ls": cmd_ls,
        "health": cmd_health,
        "rm": cmd_rm,
        "caddy-sync": cmd_caddy_sync,
        "approve": cmd_approve,
    }

    return commands[args.subcmd](args)


if __name__ == "__main__":
    sys.exit(main())
