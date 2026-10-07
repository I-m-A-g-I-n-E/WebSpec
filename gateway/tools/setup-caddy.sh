#!/usr/bin/env bash
#
# setup-caddy.sh: install Caddy (with the rate-limit plugin) as the loopback-only reverse proxy
# in front of the WebSpec gateway, running as the dedicated `caddy` system user.
#
#   cloudflared -> 127.0.0.1:7001 / [::1]:7001 (caddy-webspec.socket) -> Caddy -> gateway 127.0.0.1:7002
#   cloudflared -> 127.0.0.1:7003 / [::1]:7003 (caddy-webspec.socket) -> Caddy -> direct sites
#
# This meets DP-5, DP-6 and DP-9 (docs/spec/audit-deployment.md) and keeps DP-3 possible.
# systemd holds Caddy's listening sockets, so the ports stay bound while Caddy restarts or
# crashes.
# Caddy forwards only hosts that have a site block (any other Host gets 421) and passes the Host
# header through unchanged. Its logs contain no query strings and no headers. Its admin API is
# a unix socket that only root and caddy can open. The gateway's services are on :7001, the only
# port of Caddy's that the agent's egress allow-list may name. Direct sites (webspec-ctl add
# --direct), which bypass the gateway, are on :7003, which the allow-list must leave out.
#
# Steps (safe to re-run). Until the new configuration has validated, nothing changes but the
# creation of the caddy user and its empty state directory:
#   0. Refuse a checkout (or an installed gateway, or a prebuilt caddy) that a user other than
#      root can change. Plan the configuration, and refuse one that would stop serving a host
#      that Caddy serves today, or might (a file whose hosts cannot be read); ALLOW_SHRINK=1
#      allows it. Note what each of those hosts answers.
#   1. Build Caddy (pinned versions) with xcaddy and github.com/mholt/caddy-ratelimit, or take
#      a prebuilt one. Root never runs it.
#   2. Create the `caddy` system user if it is missing.
#   3. As caddy, check that the new binary has the modules the configuration uses. Generate the
#      configuration (webspec.caddy) into a staging directory and validate it, as caddy, with
#      the new binary.
#   4. Keep /etc/caddy/Caddyfile and every entry of conf.d aside, as they are (hard links). Move
#      aside what an earlier setup left behind: its logs, which may hold query strings and
#      credential headers, and every site block that webspec.caddy did not write or that others
#      can write (the direct sites among them are regenerated). Write /etc/caddy/Caddyfile and a
#      site block for every service in the gateway config, validate them where they are, as
#      caddy, with the new binary, and only then install it. If writing, validating or
#      installing fails, or the script stops in between, the previous files go back, and Caddy
#      is neither reloaded nor restarted: its next start loads what it runs now. Once the new
#      binary is in place, a stop keeps the new configuration, which validated with it.
#   5. Install gateway/deploy/linux/caddy-webspec.{socket,service}, keeping a copy of a replaced
#      unit that this repository did not ship; start, restart or reload. If that copy cannot be
#      made while the binary is the one it was, or a reload fails, the previous files go back
#      too, as neither the binary nor the units changed; after a failed reload, Caddy reloads
#      them. Any other failure from here on, or a stop, leaves the new configuration, and says
#      what is on disk. When the ports change hands (a first install, a Caddy that bound them
#      itself, or a socket unit that lost some), cloudflared.service is stopped until systemd
#      holds every listener, so no other process can receive the tunnel's traffic. That switch
#      runs as a transient unit of its own (webspec-caddy-cutover-*), so that it finishes, and
#      starts the tunnel again, even if this script dies: a session that came in through the
#      tunnel (cloudflared access ssh) drops when the tunnel stops. Its output goes to
#      /run/webspec-caddy-cutover.*/log, which stays there unless this script lived to show a
#      successful switch.
#   6. Verify the listeners, the catch-alls, and that every host Caddy served before is served
#      as well as it was. A session that dropped in step 5 misses this: run the script again
#      once the tunnel is back.
#
# The listeners follow the kernel the script runs on. Without IPv6 (booted with
# ipv6.disable=1), systemd ignores the socket unit's [::1] lines and passes the IPv4 sockets
# only, and the configuration binds those. Run the script again after turning IPv6 on or off at
# boot. Booted without IPv6, a configuration written while there was IPv6 binds sockets that
# Caddy never receives, so it cannot start: caddy-webspec.service says so ("caddy-webspec:
# /etc/caddy/Caddyfile binds the [::1] listeners, but this kernel has no IPv6"), and so does
# webspec-ctl health. Booted with IPv6 again, systemd holds [::1]:7001 and [::1]:7003, but a
# configuration written without IPv6 does not serve them: connections there wait unanswered
# until the script runs again, and webspec-ctl health says so. Turning IPv6 off with sysctl
# (net.ipv6.conf.*.disable_ipv6) needs no re-run: systemd still binds [::1] (FreeBind). An
# earlier version of this script gave a kernel without IPv6 a drop-in that dropped the [::1]
# lines for good, so that once the host booted with IPv6 nothing held [::1]:7001 and
# [::1]:7003, and any local user could listen there; the script removes it.
#
# Root runs this script, installs Caddy's units from gateway/deploy/linux and, until the gateway
# is installed in /opt/webspec/venv, runs its configuration generator from gateway/webspec:
# whoever can change them runs code as root (DP-1, DP-4). Like gateway/deploy/linux/install.sh,
# the script refuses a checkout that a user other than root can change (anything in gateway/,
# or a directory above it), but it cannot vouch for itself: run it from a clone that only root
# can change, such as a fresh clone of a release you have reviewed, never from the agent's
# working tree. It changes to / before it runs anything, so the working directory does not
# matter. It does not touch the gateway's own unit; install the gateway with install.sh.
#
# Usage: sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
#        sudo bash /opt/webspec/src/gateway/tools/setup-caddy.sh
#        sudo WEBSPEC_DOMAIN=example.com bash /opt/webspec/src/gateway/tools/setup-caddy.sh
#
# Environment (sudo drops these unless they are given on its command line, as above):
#   WEBSPEC_DOMAIN        Public domain; empty for loopback names (*.localhost) only.
#                         Default: the domain the current configuration records
#                         (/etc/caddy/Caddyfile), none included, so a re-run keeps it, or that
#                         the site blocks of an earlier setup serve; where nothing is recorded,
#                         on a production host (/etc/webspec/config.json exists), a non-empty
#                         WEBSPEC_DOMAIN in /etc/webspec/gateway.env; else none. If gateway.env
#                         names another public domain than the recorded one, the script stops
#                         and asks for WEBSPEC_DOMAIN.
#   WEBSPEC_CONFIG        Gateway config with mcpServers. Default: /etc/webspec/config.json on a
#                         production host. Elsewhere there is no default: name the config the
#                         gateway reads, as in sudo WEBSPEC_CONFIG=/home/<user>/.claude.json ...
#   ALLOW_SHRINK          1 lets the new configuration stop serving hosts that Caddy serves
#                         today, and set aside files whose hosts cannot be read. Without it the
#                         script names them and stops, changing nothing.
#   WEBSPEC_CADDY_BINARY  A prebuilt caddy that includes http.handlers.rate_limit. Setting it
#                         skips the build. Root installs it as /usr/local/bin/caddy, which runs
#                         every request and which webspec-ctl runs, so it must be a file that
#                         only root can change, in directories that only root can change, as
#                         written and after links (for example: sudo install -o root -g root
#                         -m 0755 ./caddy /root/caddy, once you have checked it). The script
#                         refuses any other, and runs it as caddy only, never as root.
#   CADDY_VERSION, CADDY_RATELIMIT_VERSION, XCADDY_VERSION
#                         What to build. Defaults: v2.11.7, v0.1.0, v0.4.7.
#   GO_VERSION, GO_SHA256 The Go to download when no Go >= 1.21 is installed; it is used for
#                         the build only. Default: 1.27.1, whose checksums are built in.
#                         Another version needs GO_SHA256 from https://go.dev/dl/.
#
set -euo pipefail
umask 022
# Root runs nothing found through a directory that another user may write (DP-1, DP-4).
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
    exit 0
fi

CADDY_PORT=7001
DIRECT_PORT=7003
GATEWAY_PORT=7002
CADDY_BIN=/usr/local/bin/caddy
CADDYFILE=/etc/caddy/Caddyfile
CONF_DIR=/etc/caddy/conf.d
DISABLED_DIR=/etc/caddy/conf.d.disabled
LOG_DIR=/var/log/caddy
STATE_DIR=/var/lib/caddy-webspec
ADMIN_SOCKET=/run/caddy-webspec/admin.sock
UNIT=caddy-webspec.service
SOCKET=caddy-webspec.socket
TUNNEL=cloudflared.service
UNIT_DIR=/etc/systemd/system
# Written by an earlier version of this script on a kernel without IPv6; step 5 removes it.
IPV4_ONLY_DROPIN=${UNIT_DIR}/${SOCKET}.d/10-ipv4-only.conf
PRODUCTION_CONFIG=/etc/webspec/config.json
VENV=/opt/webspec/venv
VENV_PYTHON=${VENV}/bin/python
VENV_CTL=${VENV}/bin/webspec-ctl
CADDY_VERSION="${CADDY_VERSION:-v2.11.7}"
RATELIMIT_VERSION="${CADDY_RATELIMIT_VERSION:-v0.1.0}"
XCADDY_VERSION="${XCADDY_VERSION:-v0.4.7}"
GO_VERSION="${GO_VERSION:-1.27.1}"
RATELIMIT_PLUGIN=github.com/mholt/caddy-ratelimit
STAMP="$(date +%Y%m%d-%H%M%S)"
# The same environment as the unit, so that a validation run as caddy behaves like the service.
CADDY_ENV=(XDG_DATA_HOME="${STATE_DIR}/data" XDG_CONFIG_HOME="${STATE_DIR}/config")
# SHA-256 of every caddy-webspec.service and caddy-webspec.socket this repository has shipped. A
# replaced unit that is none of them (the earlier setup generated one per host, with the login
# user as User=, or someone edited it) is copied aside first. When a unit changes, add the
# new hash; tests/test_caddy_setup_script.py checks that the current ones are listed.
SHIPPED_UNITS="
2da8a9b0d0790bef2fd997d0babc37ad3afa265d5425f885e11c42e16064415b
ed2a472229595cbdb4653156abc13b8c40c5a2631cc75a71b833a29003b394be
05a88914b23a8ee508463229a807e84a25ced428a95fc2ae5e4fdc0a7a8b22fb
50119b489028810f46f36c85307ac6bc0f243b89f6d37bcc631473755bacbdf2
097ec60a01551bfd06b32f51b5586c96cfcbfac17250c90099ccc68b2bdb4575
976e070fd77df491673660f3e9c1362aa1915deae27eab9b37f0ca775ac8c0c7
80a5b58aa601aabee4abe4e674f78e4fc9b7887e84bd949a55b6fbc6029b7899
3b9c6999271a9d522274474fe13316e17e3d6fb059ee6dcf6e9a81ab5d12b55e
5b7bb2dc0544022efff712d4e0309e94ca9c853879f86597a9d7226f577d3730
3316b5d5fd7bdeb32e9e68da4ac833cbad5b9501e26a6c295739b176f795251b
13382c7f2e7ffee5770c23e368468cd7393117fc5e5fd50234baa7304103574a
"

die() {
    ON_STOP=""  # this message says why: cleanup adds none
    echo "setup-caddy: $*" >&2
    exit 1
}
warn() { echo "Warning: $*" >&2; }

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
GATEWAY_DIR="$(dirname -- "$SCRIPT_DIR")"
UNIT_SRC_DIR="${GATEWAY_DIR}/deploy/linux"

# ── 0. Who can change what root runs (DP-1, DP-4) ──
# changeable_by_others [-L] PATH...: prints each path through which a user other than root could
# change what root runs or installs from PATH, as install.sh does for its checkout: everything
# in PATH's tree that root does not own or that its group or others can write; every symlink
# there, which could point anywhere (with -L, what each link leads to instead: a venv links its
# python to the system's); and every directory above PATH, sticky ones such as /tmp included
# (whoever can swap an entry there can swap the tree). For gateway/, that covers this script's
# directory, the units in gateway/deploy/linux, and gateway/webspec with everything else on
# Python's path when the generator comes from this checkout. Fails if find cannot look.
# (-perm -020 -o -perm -002: group- or world-writable, in a form every find accepts.)
changeable_by_others() {
    local follow=0 path dir
    if [ "$1" = -L ]; then
        follow=1
        shift
    fi
    for path in "$@"; do
        if [ "$follow" = 1 ]; then
            find -L "$path" \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print || return 1
        else
            find "$path" \( -type l -o ! -uid 0 -o -perm -020 -o -perm -002 \) -print || return 1
        fi
        dir="$path"
        while [ "$dir" != / ]; do
            dir="$(dirname -- "$dir")"
            find "$dir" -maxdepth 0 \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print || return 1
        done
    done
}

# refuse_checkout PATHS explains why the checkout is refused, showing the first few PATHS.
refuse_checkout() {
    local path shown=0 total
    total="$(grep -c '' <<<"$1")"
    echo "setup-caddy: refusing to run from $(dirname -- "$GATEWAY_DIR"): users other than root can change it." >&2
    cat >&2 <<EOF
Root runs this script, installs Caddy's units from ${UNIT_SRC_DIR} and, until the gateway is
installed in /opt/webspec/venv, runs Python from ${GATEWAY_DIR}, so whoever can change them runs
code as root (DP-1, DP-4). Not owned by root, or writable by others:
EOF
    while IFS= read -r path && [ "$shown" -lt 10 ]; do
        printf '  %s\n' "$(ls -ld -- "$path" 2>&1)" >&2
        shown=$((shown + 1))
    done <<<"$1"
    if [ "$total" -gt "$shown" ]; then
        printf '  (and %d more)\n' "$((total - shown))" >&2
    fi
    cat >&2 <<'EOF'
Run it from a clone that only root can change, such as a fresh clone of a release you have
reviewed:
  sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
  sudo bash /opt/webspec/src/gateway/tools/setup-caddy.sh
EOF
}

# A run without root also reports an untrusted checkout, so that both are fixed in one go.
failed=0
if [ "$(id -u)" -ne 0 ]; then
    echo "setup-caddy: run as root: sudo bash $0" >&2
    failed=1
fi
if ! untrusted="$(changeable_by_others "$GATEWAY_DIR" | awk '!seen[$0]++')"; then
    echo "setup-caddy: cannot check who can change ${GATEWAY_DIR}" >&2
    failed=1
elif [ -n "$untrusted" ]; then
    refuse_checkout "$untrusted"
    failed=1
fi
[ "$failed" = 0 ] || exit 1

# Make the caller's relative paths absolute, then leave their working directory: Python runs as
# root below, and a directory the agent can write must never be on its sys.path.
absolute() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}
# The gateway config. Reading none would drop every service's site block, so a host without the
# production config names one explicitly; there is no fallback to a file a user can write (F9).
if [ -n "${WEBSPEC_CONFIG:-}" ]; then
    CONFIG="$(absolute "$WEBSPEC_CONFIG")"
elif [ -f "$PRODUCTION_CONFIG" ]; then
    CONFIG=$PRODUCTION_CONFIG
else
    die "${PRODUCTION_CONFIG} does not exist, so this is not a production host (gateway/deploy/linux/install.sh creates it), and WEBSPEC_CONFIG is not set. Name the config that the gateway reads, for example: sudo WEBSPEC_CONFIG=/home/<user>/.claude.json bash $0"
fi
[ -f "$CONFIG" ] || die "the gateway config ${CONFIG} (WEBSPEC_CONFIG) does not exist"
case "${ALLOW_SHRINK:-}" in
    1) SHRINK_FLAG=--allow-shrink ;;
    "" | 0) SHRINK_FLAG="" ;;
    *) die "ALLOW_SHRINK must be 1, or unset" ;;
esac
PREBUILT=""
if [ -n "${WEBSPEC_CADDY_BINARY:-}" ]; then
    PREBUILT="$(absolute "$WEBSPEC_CADDY_BINARY")"
    # Root installs it as /usr/local/bin/caddy, which serves every request and which webspec-ctl
    # runs: whoever can replace it before the copy in step 1 decides what that is (DP-1, DP-4).
    # Like the checkout, the file and every directory above it, as written and where its links
    # lead, must be root's alone.
    [ -f "$PREBUILT" ] || die "WEBSPEC_CADDY_BINARY: ${PREBUILT} is not a file"
    if ! untrusted="$(changeable_by_others -L "$PREBUILT" "$(readlink -f -- "$PREBUILT")" | awk '!seen[$0]++')"; then
        die "cannot check who can change ${PREBUILT}"
    elif [ -n "$untrusted" ]; then
        echo "setup-caddy: refusing the prebuilt caddy ${PREBUILT} (WEBSPEC_CADDY_BINARY): users other than root can change it (DP-1, DP-4):" >&2
        while IFS= read -r path; do printf '  %s\n' "$(ls -ld -- "$path" 2>&1)" >&2; done <<<"$(head -n 10 <<<"$untrusted")"
        die "put a caddy that you have checked where only root can change it, for example with sudo install -o root -g root -m 0755 ./caddy /root/caddy, then run this again with WEBSPEC_CADDY_BINARY=/root/caddy"
    fi
fi
cd /

command -v systemctl >/dev/null || die "systemd is required"
command -v systemd-run >/dev/null || die "systemd-run (systemd) is required"
command -v runuser >/dev/null || die "runuser (util-linux) is required"
for f in "${UNIT_SRC_DIR}/${UNIT}" "${UNIT_SRC_DIR}/${SOCKET}"; do
    [ -f "$f" ] || die "missing $f"
done

# webspec.caddy generates the configuration. Prefer the root-owned gateway install, which is the
# code `webspec-ctl` runs; fall back to this checkout if the gateway is not installed yet.
if [ -x "$VENV_PYTHON" ]; then
    # install.sh makes the venv root's. One that another user can change is not run as root,
    # and the checkout does not stand in for it either: someone has tampered with the install.
    if ! untrusted="$(changeable_by_others -L "$VENV" "$(readlink -f -- "$VENV_PYTHON")" | awk '!seen[$0]++')"; then
        die "cannot check who can change ${VENV}"
    elif [ -n "$untrusted" ]; then
        echo "setup-caddy: refusing to run the gateway installed in ${VENV}: users other than root can change it (DP-1, DP-4):" >&2
        while IFS= read -r path; do printf '  %s\n' "$(ls -ld -- "$path" 2>&1)" >&2; done <<<"$(head -n 10 <<<"$untrusted")"
        die "reinstall the gateway as root from a clone that only root can change (gateway/deploy/linux/install.sh), then run this again"
    fi
    PYTHON=$VENV_PYTHON
    PY_SRC=""
    PY_SOURCE=$VENV
else
    PYTHON="$(command -v python3)" || die "python3 is required"
    PY_SRC=$GATEWAY_DIR
    PY_SOURCE="${GATEWAY_DIR} (the gateway is not installed in /opt/webspec/venv yet)"
fi
# py ARGS...: run webspec.caddy's command line. -I (isolated mode) keeps the working directory,
# PYTHON* variables and user site-packages off sys.path: nothing but the gateway code runs as root.
py() {
    "$PYTHON" -I -c 'import sys
src = sys.argv.pop(1)
if src:
    sys.path.insert(0, src)
from webspec.caddy import main
sys.exit(main(sys.argv[1:]))' "$PY_SRC" "$@"
}
# The sockets systemd passes on this kernel, for the configuration to bind: both families, or the
# IPv4 ones only on a kernel without IPv6. An older webspec.caddy has no `listeners`, and
# generates for another layout.
LISTENERS="$(py listeners 2>/dev/null)" \
    || die "webspec.caddy from ${PY_SOURCE} predates this script; upgrade the gateway first (gateway/deploy/linux/install.sh)"
case "$LISTENERS" in
    dual) EXPECTED_LISTENERS="127.0.0.1:${CADDY_PORT} 127.0.0.1:${DIRECT_PORT} [::1]:${CADDY_PORT} [::1]:${DIRECT_PORT}" ;;
    ipv4) EXPECTED_LISTENERS="127.0.0.1:${CADDY_PORT} 127.0.0.1:${DIRECT_PORT}" ;;
    *) die "webspec.caddy from ${PY_SOURCE} printed an unknown socket layout" ;;
esac
domain_line="$(py domain)" || die "could not determine the public domain (see above); give it as WEBSPEC_DOMAIN"
DOMAIN="${domain_line%%$'\t'*}"
DOMAIN_SOURCE="${domain_line#*$'\t'}"
# The webspec-ctl command that works on this host, for the hints at the end.
CTL_ENV=""
if [ "$CONFIG" != "$PRODUCTION_CONFIG" ]; then
    CTL_ENV="WEBSPEC_CONFIG=$(printf '%q' "$CONFIG") "
fi
if [ -x "$VENV_CTL" ]; then
    CTL="sudo ${CTL_ENV}${VENV_CTL}"
else
    CTL="sudo ${CTL_ENV}env -C $(printf '%q' "$GATEWAY_DIR") python3 -m webspec.ctl"
fi
# Where the gateway on this host reads WEBSPEC_DOMAIN, which it does only when it starts, and how
# to restart it (F31): the production unit (install.sh), another system unit of that name (the
# setup before gateway/deploy/), or the development unit, which runs as a login user
# (gateway/systemd/). Read only: this script never touches the gateway's unit.
if [ -f "$PRODUCTION_CONFIG" ]; then
    GATEWAY_SETTINGS=/etc/webspec/gateway.env
    RESTART_GATEWAY="sudo systemctl restart webspec-gateway"
elif [ "$(systemctl show -p LoadState --value webspec-gateway 2>/dev/null)" = loaded ]; then
    GATEWAY_SETTINGS="the environment of its system unit (systemctl cat webspec-gateway)"
    RESTART_GATEWAY="sudo systemctl restart webspec-gateway"
else
    GATEWAY_SETTINGS="the development gateway's ~/.webspec/gateway.env"
    RESTART_GATEWAY="as the user that runs it: systemctl --user restart webspec-gateway"
fi

# probe HOST PORT: the status of GET / with that Host at 127.0.0.1:PORT; 000 if nothing answers.
probe() {
    local code
    code="$(curl -q -s -o /dev/null -m 5 -w '%{http_code}' -H "Host: $1" "http://127.0.0.1:$2/")" || code=000
    printf '%s\n' "$code"
}
# served CODE: a status that some site block gave (not "-", unprobed; 000, no answer; or 421, a catch-all's).
served() {
    case "$1" in
        - | 000 | 421) return 1 ;;
    esac
}
# verify_hosts SERVED: probe each host Caddy served before this run again, where it is served now,
# and compare (F9). SERVED has a line per host: the host, its port now, kept|moved|left-direct|
# dropped (moved: to the direct listener; left-direct: from it, as a gateway service replaced a
# direct site), and its status before. Fails if a host that stays is no longer served, or went
# from below 500 to a 5xx; a dropped one should get the catch-all's 421.
verify_hosts() {
    local host port state was now note failed=0
    while IFS=$'\t' read -r host port state was; do
        now="$(probe "$host" "$port")"
        note=""
        if [ "$state" = dropped ]; then
            if [ "$now" = 421 ]; then note="no longer served, as ALLOW_SHRINK=1 allowed"; fi
        elif ! served "$now"; then
            note="NOT SERVED"
            failed=1
        elif served "$was" && [ "$was" -lt 500 ] && [ "$now" -ge 500 ]; then
            note="FAILS NOW: check what it proxies to"
            failed=1
        elif [ "$state" = moved ]; then
            note="now on the direct listener (127.0.0.1:${port})"
        elif [ "$state" = left-direct ]; then
            note="now through the gateway (127.0.0.1:${port}), no longer a direct site"
        fi
        printf '    %-40s %s -> %s%s\n' "$host" "$was" "$now" "${note:+ ${note}}"
    done <"$1"
    [ "$failed" = 0 ]
}

WORK="$(mktemp -d)"
STAGE=""
# What step 4 keeps of the configuration (keep_previous), and whether a run that stops now puts
# it back: from the first write until the new configuration and its caddy are installed. Once
# the new caddy is in place (binary_changed, and the file says so), a stop keeps the new
# configuration instead, which validated with that caddy, and says what is on disk (ON_DISK).
PREVIOUS=""
put_back_on_exit=0
binary_changed=0
ON_DISK=""
# What cleanup says when this script stops without a message of its own (a signal, or a command
# that fails under set -e), from step 5 on: what is on disk, and what to do. Every exit that says
# why clears it (die).
ON_STOP=""
cleanup() {
    if [ "$put_back_on_exit" = 1 ]; then
        if [ "$binary_changed" = 1 ] && cmp -s "${WORK}/caddy" "$CADDY_BIN"; then
            echo "setup-caddy: stopped once ${CADDY_BIN} had been replaced. ${ON_DISK}. ${NOT_STARTED}. Run this script again." >&2 || true
        else
            if [ "$binary_changed" = 1 ]; then rm -f -- "${CADDY_BIN}.new" || true; fi
            put_back_and_say "stopped before the new configuration was installed" || true
        fi
    elif [ -n "$ON_STOP" ]; then
        echo "setup-caddy: stopped before it had finished. ${ON_STOP}" >&2 || true
    fi
    rm -rf "$WORK"
    if [ -n "$STAGE" ]; then rm -rf "$STAGE"; fi
    if [ -n "$PREVIOUS" ]; then rm -rf "$PREVIOUS"; fi
}
trap cleanup EXIT
# A signal waits for the command that runs to finish: bash runs a trap's action only then, so
# cleanup judges what that command left. Untrapped, bash would run cleanup at once, while the
# command that writes the configuration or installs caddy went on writing, and put the previous
# files back under a configuration still being written.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "=== WebSpec Caddy setup ==="
echo "  Caddy:     127.0.0.1:${CADDY_PORT} (services) and :${DIRECT_PORT} (direct sites)$([ "$LISTENERS" = ipv4 ] || echo ', on [::1] too'), sockets held by systemd, as user caddy"
echo "  Gateway:   127.0.0.1:${GATEWAY_PORT}"
echo "  Domain:    ${DOMAIN:-none: *.localhost names only} (${DOMAIN_SOURCE})"
echo "  Config:    ${CONFIG}"
echo "  Generator: webspec.caddy from ${PY_SOURCE}"
echo ""

echo "=== 0. Plan"
plan_status=0
py plan --config "$CONFIG" --domain "$DOMAIN" --listeners "$LISTENERS" --gateway-port "$GATEWAY_PORT" \
    --caddy-port "$CADDY_PORT" --hosts-out "${WORK}/hosts" ${SHRINK_FLAG:+"$SHRINK_FLAG"} || plan_status=$?
case "$plan_status" in
    0) ;;
    3) die "nothing was changed. To go ahead anyway, and stop serving what is named above, run this again with ALLOW_SHRINK=1. If those hosts should stay, check the public domain (WEBSPEC_DOMAIN) and the gateway config (WEBSPEC_CONFIG) above." ;;
    *) die "could not plan the configuration (see above); nothing was changed" ;;
esac
# What every host Caddy serves today answers now, for step 6 to compare (F9).
if [ -s "${WORK}/hosts" ] && command -v curl >/dev/null; then
    while IFS=$'\t' read -r host port state before_port; do
        was=-
        if [ "$before_port" != - ]; then was="$(probe "$host" "$before_port")"; fi
        printf '%s\t%s\t%s\t%s\n' "$host" "$port" "$state" "$was"
    done <"${WORK}/hosts" >"${WORK}/served"
fi

# The setup before this script ran Caddy as the login user and logged without filters, to the
# journal too (DP-6); it is reported at the end, once replaced.
earlier_setup=0
if [ -f "${UNIT_DIR}/${UNIT}" ] && ! grep -qx 'User=caddy' "${UNIT_DIR}/${UNIT}"; then
    earlier_setup=1
fi
if [ -f "$CADDYFILE" ]; then
    case "$(head -n 1 -- "$CADDYFILE")" in
        "# Generated by webspec.caddy"*) ;;
        *) earlier_setup=1 ;;
    esac
fi
# When systemd takes the ports over (step 5), cloudflared.service is stopped; this script cannot
# stop a cloudflared that runs some other way, which goes on forwarding while the ports are free
# for a moment. warn_foreign_tunnels names those: here already when the socket unit is not active,
# so certain to be replaced, and in step 5 for any other switch.
foreign_warned=0
warn_foreign_tunnels() {
    local tunnel_pid others
    foreign_warned=1
    command -v pgrep >/dev/null || return 0
    tunnel_pid="$(systemctl show -p MainPID --value "$TUNNEL" 2>/dev/null)" || tunnel_pid=0
    others="$(pgrep -x cloudflared || true)"
    others="$(grep -vx -- "${tunnel_pid:-0}" <<<"$others" | tr '\n' ' ' || true)"
    if [ -n "${others// /}" ]; then
        warn "cloudflared runs outside ${TUNNEL} (pid ${others% }). While systemd takes :${CADDY_PORT} and :${DIRECT_PORT} over, the ports are free for a moment, and this script can stop only ${TUNNEL}: stop that cloudflared first, and start it again when the script has finished."
    fi
}
if ! systemctl is-active --quiet "$SOCKET"; then
    warn_foreign_tunnels
fi

# ── 1. Caddy with the rate-limit plugin ──
go_is_recent() { # Go >= 1.21 fetches the newer toolchain that current Caddy requires.
    local v
    v="$("$1" env GOVERSION 2>/dev/null)" || return 1
    v="${v#go}"
    [ "$(printf '%s\n' 1.21 "$v" | sort -V | sed -n 1p)" = "1.21" ]
}

go_sha256() { # go_sha256 ARCH: the published checksum of the Go archive (https://go.dev/dl/)
    case "${GO_VERSION}-$1" in
        1.27.1-amd64) echo 63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445 ;;
        1.27.1-arm64) echo 3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec ;;
        *) echo "${GO_SHA256:-}" ;;
    esac
}

build_caddy() { # build_caddy OUTPUT
    local go goarch sum
    # A hermetic build: no go env file, module cache or build cache from a home directory, and
    # every module checked against the Go checksum database (sum.golang.org).
    export GOENV=off GOPATH="${WORK}/gopath" GOCACHE="${WORK}/gocache" TMPDIR="${WORK}/tmp" GOTOOLCHAIN=auto
    unset GOFLAGS GONOSUMDB GONOSUMCHECK GOINSECURE GOPRIVATE GONOPROXY
    mkdir -p "$TMPDIR"
    go="$(command -v go || true)"
    if [ -z "$go" ] && [ -x /usr/local/go/bin/go ]; then go=/usr/local/go/bin/go; fi
    if [ -z "$go" ] || ! go_is_recent "$go"; then
        case "$(uname -m)" in
            x86_64) goarch=amd64 ;;
            aarch64 | arm64) goarch=arm64 ;;
            *) die "no Go download for $(uname -m); install Go >= 1.21 or set WEBSPEC_CADDY_BINARY" ;;
        esac
        sum="$(go_sha256 "$goarch")"
        [ -n "$sum" ] || die "no checksum for Go ${GO_VERSION} linux-${goarch}: set GO_SHA256 (https://go.dev/dl/)"
        echo "Downloading Go ${GO_VERSION} (linux-${goarch}) for the build..."
        # -q first: no .curlrc (of whatever HOME this runs with) can change what is fetched.
        curl -q -fsSL --proto '=https' --tlsv1.2 -o "${WORK}/go.tar.gz" \
            "https://go.dev/dl/go${GO_VERSION}.linux-${goarch}.tar.gz"
        printf '%s  %s\n' "$sum" "${WORK}/go.tar.gz" | sha256sum -c --quiet - \
            || die "the Go ${GO_VERSION} archive does not match its published checksum"
        tar -C "$WORK" -xzf "${WORK}/go.tar.gz"
        go="${WORK}/go/bin/go"
    fi
    PATH="$(dirname -- "$go"):${PATH}"
    export PATH
    echo "Go: $("$go" version)"
    echo "Building Caddy ${CADDY_VERSION} with ${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION} (xcaddy ${XCADDY_VERSION})..."
    GOBIN="${WORK}/bin" "$go" install "github.com/caddyserver/xcaddy/cmd/xcaddy@${XCADDY_VERSION}"
    (cd "$WORK" && XCADDY_SETCAP=0 "${WORK}/bin/xcaddy" build "$CADDY_VERSION" \
        --with "${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}" --output "$1")
}

echo "=== 1. Caddy"
if [ -n "$PREBUILT" ]; then
    echo "Using the prebuilt ${PREBUILT}"
    cp -- "$PREBUILT" "${WORK}/caddy"
else
    build_caddy "${WORK}/caddy"
fi
chmod 0755 "${WORK}/caddy"
# Not run here: step 3 checks it as caddy, once that user exists.

# ── 2. The caddy system user ──
echo "=== 2. The caddy user"
if ! getent group caddy >/dev/null; then
    groupadd --system caddy
fi
if ! getent passwd caddy >/dev/null; then
    useradd --system --gid caddy --home-dir /var/lib/caddy --no-create-home \
        --shell "$(command -v nologin || echo /usr/sbin/nologin)" --comment "Caddy web server" caddy
    echo "Created system user caddy"
fi
case "$(getent passwd caddy | cut -d: -f7)" in
    */nologin | */false) ;;
    *) warn "the caddy user has a login shell; a service user should have /usr/sbin/nologin" ;;
esac
install -d -m 0700 -o caddy -g caddy "$STATE_DIR"

# ── 3. Stage and validate ──
echo "=== 3. Configuration"
# Readable by caddy, which validates it. It holds no secrets.
STAGE="$(mktemp -d /tmp/webspec-caddy.XXXXXX)"
chgrp caddy "$STAGE"
chmod 0750 "$STAGE"
install -m 0755 "${WORK}/caddy" "${STAGE}/caddy"
install -d -m 0700 -o caddy -g caddy "${STAGE}/log"

# as_caddy COMMAND...: run COMMAND as caddy, in the service's environment. Root never runs the
# new caddy, a prebuilt one (WEBSPEC_CADDY_BINARY) least of all: the service runs it as caddy too.
as_caddy() {
    runuser -u caddy -- env "${CADDY_ENV[@]}" "$@"
}
# Captured first: `cmd | grep -q` can fail under pipefail when grep exits early.
modules="$(as_caddy "${STAGE}/caddy" list-modules)" || die "the new caddy's list-modules failed"
for module in http.handlers.rate_limit http.handlers.map caddy.logging.encoders.filter; do
    grep -qx "$module" <<<"$modules" || die "this caddy lacks the module ${module}"
done
echo "New Caddy: $(as_caddy "${STAGE}/caddy" version)"

py stage --config "$CONFIG" --domain "$DOMAIN" --listeners "$LISTENERS" --gateway-port "$GATEWAY_PORT" \
    --caddy-port "$CADDY_PORT" --out "$STAGE" ${SHRINK_FLAG:+"$SHRINK_FLAG"} \
    || die "could not generate the configuration; nothing was changed"

validate() { # validate CADDY CADDYFILE: as caddy, in the service's environment
    as_caddy "$1" validate --config "$2" >"${WORK}/validate.log" 2>&1
}
if ! validate "${STAGE}/caddy" "${STAGE}/Caddyfile"; then
    cat "${WORK}/validate.log" >&2
    die "the new configuration does not validate; nothing was changed"
fi
echo "The new configuration is valid."

# ── 4. Install ──
echo "=== 4. Install"
# /etc/caddy is root's: caddy and the agent can read it (no secrets), only root writes.
install -d -m 0755 -o root -g root /etc/caddy "$CONF_DIR"

# The files of the configuration that Caddy runs go back as they were if the new one cannot be
# written, does not validate where it is installed, or cannot be installed with its caddy, if
# this script stops in between (Ctrl-C, a hangup), and if Caddy does not reload it (step 5): the
# next start of Caddy then loads what it runs now, never a configuration that failed.
# keep_previous keeps them in PREVIOUS before anything here changes: another name (a hard link)
# for the Caddyfile and for every entry of conf.d as it is (links, FIFOs, other users' files,
# names the import glob does not match), so that each goes back as the very same file, owner,
# attributes and links included, and takes no space. That holds because apply only renames
# files into place and out of the way, and never writes into one. A directory, which cannot have
# another name, is copied (cp -a), as is a file that cannot be linked. PREVIOUS is root's alone,
# next to what it keeps, so that put_back only renames: its empty `written` is for what put_back
# moves out of the way. cleanup removes PREVIOUS.
keep() { # keep FROM TO: TO becomes another name of FROM, or a copy of a directory
    if [ -d "$1" ] && [ ! -L "$1" ]; then
        cp -a -- "$1" "$2"
    else
        ln -P -- "$1" "$2" 2>/dev/null || cp -a -- "$1" "$2"
    fi
}
keep_previous() {
    local entry
    PREVIOUS="$(mktemp -d /etc/caddy/previous.XXXXXX)" || return 1
    mkdir -- "${PREVIOUS}/conf.d" "${PREVIOUS}/written" "${PREVIOUS}/written/conf.d" || return 1
    had_caddyfile=0
    if [ -e "$CADDYFILE" ] || [ -L "$CADDYFILE" ]; then
        had_caddyfile=1
        keep "$CADDYFILE" "${PREVIOUS}/Caddyfile" || return 1
    fi
    for entry in "$CONF_DIR"/* "$CONF_DIR"/.[!.]* "$CONF_DIR"/..?*; do
        if [ -e "$entry" ] || [ -L "$entry" ]; then
            keep "$entry" "${PREVIOUS}/conf.d/${entry##*/}" || return 1
        fi
    done
}
# move_entries FROM TO: move every entry of the directory FROM, dot files too, into the directory
# TO, never onto an entry of TO, which stays. Sets moved and unmoved: how many it moved, and how
# many it did not (mv says why).
move_entries() {
    local entry
    moved=0 unmoved=0
    for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
        if [ -e "$entry" ] || [ -L "$entry" ]; then
            if [ ! -e "$2/${entry##*/}" ] && [ ! -L "$2/${entry##*/}" ] && mv -- "$entry" "$2/"; then
                moved=$((moved + 1))
            else
                unmoved=$((unmoved + 1))
            fi
        fi
    done
}
# put_back COPY: what COPY (keep_previous) holds, in place of what this run wrote, which goes to
# COPY/written. It only renames. What cannot be moved stays where it is, and the rest is moved
# all the same. Sets wrote and stuck, how many entries went to COPY/written and how many did not,
# and restored and left, how many came back from COPY and how many stayed there. Once all of it
# is back, this run's directory of the site blocks moved out of conf.d goes too: COPY held them.
put_back() {
    move_entries "$CONF_DIR" "$1/written/conf.d"
    wrote=$moved stuck=$unmoved
    move_entries "$1/conf.d" "$CONF_DIR"
    restored=$moved left=$unmoved
    if [ -e "$CADDYFILE" ] || [ -L "$CADDYFILE" ]; then
        if mv -- "$CADDYFILE" "$1/written/Caddyfile"; then wrote=$((wrote + 1)); else stuck=$((stuck + 1)); fi
    fi
    if [ -e "$1/Caddyfile" ] || [ -L "$1/Caddyfile" ]; then
        if [ ! -e "$CADDYFILE" ] && [ ! -L "$CADDYFILE" ] && mv -- "$1/Caddyfile" "$CADDYFILE"; then
            restored=$((restored + 1))
        else
            left=$((left + 1))
        fi
    fi
    if [ "$stuck" != 0 ] || [ "$left" != 0 ]; then return 1; fi
    if [ -e "${DISABLED_DIR}/${STAMP}" ]; then
        rm -rf -- "${DISABLED_DIR:?}/${STAMP}" \
            || warn "could not remove ${DISABLED_DIR}/${STAMP}: its files are back in ${CONF_DIR}"
        rmdir -- "$DISABLED_DIR" 2>/dev/null || true
    fi
}
# put_back_and_say REASON [CADDY]: put_back, and say so after REASON. CADDY says what became of the
# caddy binary and of Caddy; by default, step 4's: neither changed. Until the copy is all back,
# cleanup leaves it on disk, and says so should this script stop half-way. When something cannot
# be put back, the message says what is where, from what moved.
put_back_and_say() {
    local copy=$PREVIOUS what
    put_back_on_exit=0
    PREVIOUS=""
    ON_STOP="The previous configuration was being put back: ${copy} holds what is not back in ${CADDYFILE} and ${CONF_DIR}/, and ${copy}/written what this run moved out of the way. Put the previous files back before Caddy next starts."
    if put_back "$copy"; then
        PREVIOUS=$copy
        if [ "$had_caddyfile" = 1 ]; then
            what="The previous ${CADDYFILE} and ${CONF_DIR}/ were put back as they were"
        elif [ -e "${copy}/written/Caddyfile" ] || [ -L "${copy}/written/Caddyfile" ]; then
            what="There was no ${CADDYFILE} before: the one this run wrote was removed, and ${CONF_DIR}/ was put back as it was"
        else
            what="There was no ${CADDYFILE} before, nor is there one now, and ${CONF_DIR}/ was put back as it was"
        fi
        echo "setup-caddy: $1. ${what}; ${2:-${CADDY_BIN} was not replaced, and Caddy was not reloaded or restarted}." >&2
        ON_STOP=""
        return 0
    fi
    if [ "$left" = 0 ]; then  # all that was there before is back: some of this run's files stayed
        what="Not all of what this run wrote could be moved out of the way (see above): what could not is still in place"
        if [ "$wrote" != 0 ]; then what="${what}, and ${copy}/written holds the rest"; fi
        what="${what}. Caddy was not restarted: remove those files before it next starts"
    elif [ "$wrote" = 0 ] && [ "$restored" = 0 ]; then
        what="The previous configuration could not be put back (see above): what this run wrote is still in place, and ${copy} holds the previous configuration. Caddy was not restarted: put the previous files back before it next starts"
    else
        what="Not all of the previous configuration could be put back (see above): ${copy} holds what is not back in ${CADDYFILE} and ${CONF_DIR}/"
        if [ "$wrote" != 0 ]; then what="${what}, and ${copy}/written what this run moved out of the way"; fi
        if [ "$stuck" != 0 ]; then what="${what}; what it could not move is still in place"; fi
        what="${what}. Caddy was not restarted: put the previous files back before it next starts"
    fi
    echo "setup-caddy: $1. ${what}." >&2
    ON_STOP=""
    return 1
}
put_back_and_die() {
    put_back_and_say "$@" || true
    exit 1
}
keep_previous || die "could not keep ${CADDYFILE} and ${CONF_DIR}/ aside (see above); the configuration was not changed"

if [ -f "$CADDYFILE" ]; then
    first_line="$(head -n 1 -- "$CADDYFILE")"
    case "$first_line" in
        "# Generated by webspec.caddy"*) ;;
        *)
            cp -p -- "$CADDYFILE" "${CADDYFILE}.bak.${STAMP}"
            echo "Saved the previous ${CADDYFILE} as ${CADDYFILE}.bak.${STAMP}"
            ;;
    esac
fi

# An earlier setup ran Caddy as the login user, which wrote unfiltered logs (query strings,
# guard tags, cookies) into a directory that user owned. Neither may stay in service.
log_dir_is_current() { # caddy:caddy, not a symlink, holding only regular files of caddy
    if [ -L "$LOG_DIR" ] || [ ! -d "$LOG_DIR" ]; then return 1; fi
    [ "$(stat -c %U:%G "$LOG_DIR")" = caddy:caddy ] || return 1
    [ -z "$(find "$LOG_DIR" -mindepth 1 \( ! -type f -o ! -user caddy \) -print -quit)" ]
}
if { [ -e "$LOG_DIR" ] || [ -L "$LOG_DIR" ]; } && ! log_dir_is_current; then
    old_logs="${LOG_DIR}.before-webspec.${STAMP}"
    mv -- "$LOG_DIR" "$old_logs"
    if [ ! -L "$old_logs" ]; then
        chown root:root "$old_logs"
        chmod 0700 "$old_logs"
    fi
    warn "moved ${LOG_DIR}, left by an earlier setup, to ${old_logs} (root only). Its logs may hold query strings (GET arguments) and credential headers: review them, then delete them (rm -rf ${old_logs})."
fi
install -d -m 0750 -o caddy -g caddy "$LOG_DIR"

echo "Site blocks:"
put_back_on_exit=1
py apply --config "$CONFIG" --domain "$DOMAIN" --listeners "$LISTENERS" --gateway-port "$GATEWAY_PORT" \
    --caddy-port "$CADDY_PORT" --disabled "${DISABLED_DIR}/${STAMP}" ${SHRINK_FLAG:+"$SHRINK_FLAG"} \
    || put_back_and_die "could not write the configuration (see above)"
# The new caddy validates it, and is installed only once it has: until then, the caddy in place
# is the one that runs the previous configuration.
if ! validate "${STAGE}/caddy" "$CADDYFILE"; then
    cat "${WORK}/validate.log" >&2
    put_back_and_die "the installed configuration does not validate, although its staged copy did"
fi
echo "Wrote ${CADDYFILE} and ${CONF_DIR}/"
# What is on disk when this run leaves the new configuration in place: from the moment the new
# caddy is in place, and in step 5. It binds the listeners that only this repository's units
# pass (fd/N).
ON_DISK="On disk: the new configuration (${CADDYFILE}, ${CONF_DIR}/), which validated with ${CADDY_BIN}"
NOT_STARTED="Caddy was not reloaded or restarted, but that configuration needs this repository's units, so a start of Caddy before this script has finished (a crash, a reboot) may fail"
if ! cmp -s "${WORK}/caddy" "$CADDY_BIN"; then
    # A fresh file carries no file capabilities: Caddy needs none, as systemd binds its ports.
    # Set first: should this script stop once the mv has replaced the caddy, before it can say
    # so, cleanup finds the new one in place.
    binary_changed=1
    if ! install -m 0755 -o root -g root "${WORK}/caddy" "${CADDY_BIN}.new" || ! mv -f "${CADDY_BIN}.new" "$CADDY_BIN"; then
        rm -f -- "${CADDY_BIN}.new"
        put_back_and_die "could not install ${CADDY_BIN} (see above)"
    fi
    echo "Installed ${CADDY_BIN}"
fi
ON_STOP="${ON_DISK}. ${NOT_STARTED}. Run this script again."
put_back_on_exit=0

# ── 5. Units ──
echo "=== 5. Units"
# From here on, a failure leaves the new configuration in place: the units change, and Caddy
# restarts with them and with the caddy now installed, which the previous configuration may not
# suit. Two failures put the previous files back, as neither the caddy nor the units changed: a
# replaced unit that cannot be copied aside while the caddy is the one it was (unit_not_kept),
# and a reload that fails (below). Every other failure says what is on disk (ON_DISK), and so
# does cleanup when this script stops without a message of its own (ON_STOP).
# install_units_failed REASON: stop before Caddy is switched, restarted or reloaded.
install_units_failed() {
    die "$*. ${ON_DISK}. ${NOT_STARTED}. Fix the cause, then run this script again."
}
# unit_not_kept REASON: keep_unless_shipped failed, before any unit was written. With the caddy
# that ran the previous configuration still in place, that configuration goes back, as in step 4.
# A new caddy never validated it, so then the new one stays.
unit_not_kept() {
    if [ "$binary_changed" = 0 ]; then
        put_back_and_die "$*" "${CADDY_BIN} and the units were not changed, and Caddy was not reloaded or restarted"
    fi
    install_units_failed "$*"
}
install_if_changed() { # install_if_changed SRC DEST: installs SRC as DEST; fails if it was unchanged
    if [ -f "$2" ] && cmp -s "$1" "$2"; then return 1; fi
    # Called as a condition, where set -e is off: a failed install must still stop the script.
    # install removes DEST first, and leaves what it could write.
    install -m 0644 -o root -g root "$1" "$2" \
        || install_units_failed "could not install $2, which may now be missing or incomplete (see above)"
}
keep_unless_shipped() { # keep_unless_shipped DEST SRC: copy DEST aside before SRC replaces it, unless shipped
    local sum
    if [ ! -f "$1" ] || cmp -s "$1" "$2"; then return 0; fi
    sum="$(sha256sum <"$1")" || unit_not_kept "could not read $1 (see above)"
    if grep -qx -- "${sum%% *}" <<<"$SHIPPED_UNITS"; then return 0; fi
    cp -p -- "$1" "$1.bak.${STAMP}" || unit_not_kept "could not copy $1 aside (see above)"
    echo "Saved the previous $1, which this repository did not ship, as $1.bak.${STAMP}"
}
keep_unless_shipped "${UNIT_DIR}/${SOCKET}" "${UNIT_SRC_DIR}/${SOCKET}"
keep_unless_shipped "${UNIT_DIR}/${UNIT}" "${UNIT_SRC_DIR}/${UNIT}"
socket_changed=0
unit_changed=0
if install_if_changed "${UNIT_SRC_DIR}/${SOCKET}" "${UNIT_DIR}/${SOCKET}"; then socket_changed=1; fi
if install_if_changed "${UNIT_SRC_DIR}/${UNIT}" "${UNIT_DIR}/${UNIT}"; then unit_changed=1; fi
# The unit's [::1] lines stay, whatever the kernel. Without IPv6, systemd ignores them by itself
# (it checks /proc/net/if_inet6), and passes 127.0.0.1:7001 and 127.0.0.1:7003 as fds 3 and 4,
# which the configuration binds. With IPv6, it holds [::1]:7001 and [::1]:7003 too, so that no
# other process can take them (DP-9), even if the host was set up without IPv6. An earlier
# version of this script wrote the IPv4-only drop-in, which dropped them for good: remove it.
# The switch below restarts the socket unit, so systemd binds [::1] now, and says so if it cannot.
if [ -e "$IPV4_ONLY_DROPIN" ] || [ -L "$IPV4_ONLY_DROPIN" ]; then
    rm -f -- "$IPV4_ONLY_DROPIN" || install_units_failed "could not remove ${IPV4_ONLY_DROPIN} (see above)"
    rmdir -- "$(dirname -- "$IPV4_ONLY_DROPIN")" 2>/dev/null || true
    socket_changed=1
    echo "Removed ${IPV4_ONLY_DROPIN}, which an earlier setup wrote: systemd holds [::1] wherever the kernel has IPv6"
fi
systemctl daemon-reload || install_units_failed "systemctl daemon-reload failed (see above)"
if systemctl is-enabled --quiet caddy.service 2>/dev/null || systemctl is-active --quiet caddy.service 2>/dev/null; then
    warn "caddy.service (the distribution's unit) also reads ${CADDYFILE}; stop and disable it: systemctl disable --now caddy"
fi
systemctl enable --quiet "$SOCKET" "$UNIT" || install_units_failed "could not enable ${SOCKET} and ${UNIT} (see above)"
# Installed above, or already as shipped: the failures below leave them too.
ON_DISK="${ON_DISK}, and the units this repository ships (${UNIT_DIR}/${SOCKET}, ${UNIT})"
ON_STOP="${ON_DISK}. Run this script again."

unit_failed() {
    journalctl -u "$SOCKET" -u "$UNIT" -n 30 --no-pager >&2 || true
    die "$*"
}
# Who holds the listeners of caddy-webspec.socket. `ss -p` names every process that has a
# listening socket open: Caddy has its copies, and systemd (pid 1) keeps its own of each socket
# it holds, so a listener that systemd holds shows "systemd",pid=1 on its line.
LISTENING=""
read_listeners() { LISTENING="$(ss -Hltnp 2>/dev/null)" || LISTENING=""; }
# owners SS_LINE: the processes on an ss line, as "name (pid N, user U)". The name is the
# process's own choice: anything that is not plainly printable is shown as ?.
owners() {
    local pid name out=""
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        name="$(ps -o comm= -p "$pid" 2>/dev/null | LC_ALL=C tr -c 'A-Za-z0-9._+:@\n-' '?')" || name='?'
        out="${out:+${out}, }${name:-?} (pid ${pid}, user $(ps -o user= -p "$pid" 2>/dev/null || echo '?'))"
    done <<<"$(grep -o 'pid=[0-9]*' <<<"$1" | cut -d= -f2 | sort -un)"
    printf '%s\n' "${out:-a process ss cannot name}"
}
# unheld_listeners: each listener of the socket unit that systemd does not hold (read_listeners
# first), with whoever holds it instead.
unheld_listeners() {
    local address line
    for address in $EXPECTED_LISTENERS; do
        line="$(awk -v a="$address" '$4 == a' <<<"$LISTENING")"
        case "$line" in
            *'"systemd",pid=1,'*) ;;
            "") echo "nothing listens on ${address}" ;;
            *) echo "${address} is held by $(owners "$line")" ;;
        esac
    done
}
# systemd_holds_listeners: caddy-webspec.socket is active, which systemd allows only once it has
# bound every listener, and, where ss can tell, systemd still holds every one. An active unit
# can have lost some: a daemon-reload after its listeners changed closes the ones it no longer
# lists, and leaves the unit active. Listeners of other processes at other addresses of these
# ports (127.0.0.2:7001, say) receive none of the tunnel's traffic; step 6 reports them.
systemd_holds_listeners() {
    systemctl is-active --quiet "$SOCKET" || return 1
    command -v ss >/dev/null || return 0
    read_listeners
    [ -z "$(unheld_listeners)" ]
}
tunnel_stopped=0
bind_failed() {
    local report line
    journalctl -u "$SOCKET" -n 20 --no-pager >&2 || true
    if command -v ss >/dev/null; then
        read_listeners
        report="$(unheld_listeners)"
        while IFS= read -r line; do printf '  %s\n' "$line" >&2; done <<<"$report"
    else
        report=""
        echo "  (ss, from iproute2, is not installed: this script cannot tell who holds them)" >&2
    fi
    if grep -q ' is held by ' <<<"$report"; then
        echo "setup-caddy: systemd could not take every listener of ${SOCKET} over: the processes above hold them." >&2
    else
        echo "setup-caddy: systemd could not bind every listener of ${SOCKET} (see above)." >&2
    fi
    if [ "$tunnel_stopped" = 1 ]; then
        echo "${TUNNEL} was stopped for the switch and stays stopped, so none of them receives the tunnel's traffic." >&2
        echo "Once this script has succeeded, start it again: systemctl start ${TUNNEL}" >&2
    else
        echo "If cloudflared runs, stop it now: a process that holds 127.0.0.1:${CADDY_PORT} or 127.0.0.1:${DIRECT_PORT} receives its traffic (full URLs with GET arguments, guard tags)." >&2
    fi
    die "stop those processes, then run this script again"
}
# cutover: the switch, for a first install, a Caddy that bound the ports itself (an earlier
# setup), new sockets, or a socket unit that lost some. The ports are free for a moment between
# the stop and the start, and a process of another user that binds one then would receive the
# tunnel's traffic (DP-9): cloudflared.service is stopped until systemd holds every listener
# (F29). It runs as a transient unit (run_cutover), with nothing from this script but what
# run_cutover passes it.
cutover() {
    if systemctl is-active --quiet "$TUNNEL"; then
        systemctl stop "$TUNNEL"
        tunnel_stopped=1
        echo "Stopped ${TUNNEL} while systemd takes the ports over"
    fi
    systemctl stop "$UNIT" "$SOCKET" 2>/dev/null || true
    if ! systemctl start "$SOCKET" || ! systemd_holds_listeners; then
        bind_failed
    fi
    if [ "$tunnel_stopped" = 1 ]; then
        if systemctl start "$TUNNEL"; then
            echo "Started ${TUNNEL} again: systemd holds every listener"
        else
            warn "${TUNNEL} did not start again; start it: systemctl start ${TUNNEL}"
        fi
    fi
    systemctl start "$UNIT" || unit_failed "${UNIT} did not start"
    echo "Started ${SOCKET} and ${UNIT}"
}
# run_cutover: run cutover as a transient unit, which outlives this script. Stopping cloudflared
# drops a session that came in through the tunnel (cloudflared access ssh), and the hangup ends
# this script: a switch run here would stop half-way, the tunnel down and only the console left
# to reach the host. The unit finishes it whatever becomes of this script. Its program and its
# output are in a directory under /run that only root can read: removed once this script has
# shown a successful switch's output, and otherwise kept for the operator to read (until the
# next boot). The program is a file, not a command line: systemd would expand the $ signs of one.
CUTOVER_UNIT="webspec-caddy-cutover-${STAMP}"
run_cutover() {
    local dir
    # Its program is written in full before anything is said or stopped: a switch that cannot be
    # prepared changes nothing, and one cut short would run without what it calls.
    dir="$(mktemp -d /run/webspec-caddy-cutover.XXXXXX)" \
        || die "could not prepare the switch under /run (see above); nothing was switched. ${ON_DISK}. Fix the cause, then run this script again."
    {
        echo "# setup-caddy.sh's switch (run_cutover), run by systemd as ${CUTOVER_UNIT}.service" &&
            echo "set -euo pipefail" &&
            printf 'PATH=%q\nexport PATH\n' "$PATH" &&
            declare -p SOCKET UNIT TUNNEL EXPECTED_LISTENERS CADDY_PORT DIRECT_PORT tunnel_stopped &&
            declare -f die warn unit_failed read_listeners owners unheld_listeners systemd_holds_listeners \
                bind_failed cutover &&
            echo "cutover"
    } >"${dir}/switch.sh" \
        || die "could not prepare the switch in ${dir} (see above); nothing was switched. ${ON_DISK}. Fix the cause, then run this script again."
    if systemctl is-active --quiet "$TUNNEL"; then
        echo "The ports change hands: ${TUNNEL} stops until systemd holds every listener (F29). A session that"
        echo "came in through that tunnel drops now. The switch runs on as ${CUTOVER_UNIT}, writes to"
        echo "${dir}/log, and starts the tunnel again, unless another process holds a listener: then the"
        echo "tunnel stays down, and only the console reaches this host. Run this script again once you are back."
    fi
    ON_STOP="${ON_DISK}. The switch may still run as ${CUTOVER_UNIT}: once it has ended (journalctl -u ${CUTOVER_UNIT}), run this script again."
    if systemd-run --unit="$CUTOVER_UNIT" --description="WebSpec: systemd takes Caddy's listeners over" \
        --collect --wait --quiet --property=StandardOutput="file:${dir}/log" /bin/bash "${dir}/switch.sh"; then
        ON_STOP="${ON_DISK}. Run this script again."
        cat -- "${dir}/log"
        rm -rf -- "$dir"
        return 0
    fi
    if systemctl is-active --quiet "${CUTOVER_UNIT}.service"; then  # systemd-run was stopped, not the switch
        die "lost sight of the switch, which still runs as ${CUTOVER_UNIT} and writes to ${dir}/log. ${ON_DISK}. Run this script again once the switch has finished."
    fi
    if [ -s "${dir}/log" ]; then
        cat -- "${dir}/log" >&2
        echo "setup-caddy: the switch failed; its output, above, is kept in ${dir}/log. ${ON_DISK}. Fix the cause, then run this script again." >&2
        ON_STOP=""
        exit 1
    fi
    die "the switch (${CUTOVER_UNIT}) did not run, or did not report back: see journalctl -u ${CUTOVER_UNIT} and ${dir}. ${ON_DISK}. Once the journal shows that the switch has ended, run this script again."
}
# running_other_caddy succeeds when the Caddy that runs is not the file installed now. A run that
# stopped once the new caddy was in place left the old one running, and the next run builds the
# same caddy (binary_changed=0): it must restart Caddy, not reload it.
running_other_caddy() {
    local pid
    pid="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)" || return 1
    case "$pid" in "" | 0) return 1 ;; esac
    [ "$(stat -L -c %d:%i -- "/proc/${pid}/exe" 2>/dev/null)" != "$(stat -L -c %d:%i -- "$CADDY_BIN" 2>/dev/null)" ]
}
if [ "$socket_changed" = 1 ] || ! systemd_holds_listeners; then
    if [ "$foreign_warned" = 0 ]; then
        warn_foreign_tunnels
    fi
    run_cutover
elif [ "$binary_changed" = 1 ] || [ "$unit_changed" = 1 ] || ! systemctl is-active --quiet "$UNIT" \
    || running_other_caddy; then
    systemctl restart "$UNIT" \
        || unit_failed "${UNIT} did not restart. ${ON_DISK}. Fix what the journal above shows, then run this script again."
    echo "Restarted ${UNIT}; ${SOCKET} kept the ports bound"
else
    # Only the configuration changed. A reload that fails leaves Caddy with what it ran, unless it
    # failed only once Caddy had loaded the new configuration (it timed out): the previous files go
    # back, and Caddy reloads them, so that it runs what is on disk.
    if ! systemctl reload "$UNIT"; then
        journalctl -u "$SOCKET" -u "$UNIT" -n 30 --no-pager >&2 || true
        if put_back_and_say "${UNIT} did not reload (see above)" \
            "${CADDY_BIN} and the units were not changed, and Caddy was not restarted"; then
            ON_STOP="The previous configuration is back on disk, but Caddy may still run the new one: have it load the files on disk (sudo systemctl reload caddy-webspec)."
            if systemctl reload "$UNIT"; then
                echo "setup-caddy: reloaded ${UNIT} with the files put back: Caddy runs the previous configuration. Fix what the journal above shows, then run this script again." >&2
            else
                journalctl -u "$UNIT" -n 10 --no-pager >&2 || true
                echo "setup-caddy: ${UNIT} did not reload the files put back either (see above). If the first reload timed out, Caddy may still run the new configuration: once the journal above shows why, have it load the files on disk (sudo systemctl reload caddy-webspec), then run this script again." >&2
            fi
        fi
        ON_STOP=""
        exit 1
    fi
    echo "Reloaded ${UNIT}"
fi

# ── 6. Verify ──
echo ""
echo "=== Verification ==="
verify_failed=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    systemctl is-active --quiet "$UNIT" && break
    sleep 1
done
if systemctl is-active --quiet "$UNIT"; then
    echo "  Caddy:       running as $(ps -o user= -p "$(systemctl show -p MainPID --value "$UNIT")" 2>/dev/null || echo '?')"
else
    unit_failed "${UNIT} is not running. ${ON_DISK}. Fix what the journal above shows, then run this script again."
fi

if command -v ss >/dev/null; then
    for port in "$CADDY_PORT" "$DIRECT_PORT"; do
        addresses="$(ss -Hltn "sport = :${port}" | awk '{print $4}' | sort -u)"
        if [ -z "$addresses" ]; then
            warn "nothing listens on :${port}"
            verify_failed=1
        elif grep -Evq '^(127\.[0-9.]+|\[::1\]):' <<<"$addresses"; then
            warn "something listens on a non-loopback address at :${port}: $(tr '\n' ' ' <<<"$addresses")"
            verify_failed=1
        else
            echo "  Listeners:   $(tr '\n' ' ' <<<"$addresses")(loopback only)"
        fi
    done
    if systemd_holds_listeners; then
        echo "  Sockets:     held by systemd (${SOCKET}) and passed to Caddy"
    else
        warn "the listeners of ${SOCKET} are not all held by systemd: $(unheld_listeners | paste -sd ';' -)"
        verify_failed=1
    fi
    read_listeners
    while IFS= read -r line; do
        case "$line" in
            "" | *'"systemd",pid=1,'*) ;;
            *) warn "$(awk '{print $4}' <<<"$line") is a listener of $(owners "$line"), not of ${SOCKET}" ;;
        esac
    done <<<"$(awk -v p="$CADDY_PORT" -v q="$DIRECT_PORT" '$4 ~ (":" p "$") || $4 ~ (":" q "$")' <<<"$LISTENING")"
    if [ -n "$(ss -Hltn 'sport = :2019')" ]; then
        warn "something listens on TCP :2019 (Caddy's default admin port); this Caddy's admin API is ${ADMIN_SOCKET}"
    fi
fi

if [ -S "$ADMIN_SOCKET" ]; then
    echo "  Admin API:   ${ADMIN_SOCKET} ($(stat -c '%a %U:%G' "$ADMIN_SOCKET"), directory $(stat -c '%a %U:%G' "$(dirname -- "$ADMIN_SOCKET")"))"
else
    warn "admin socket ${ADMIN_SOCKET} not found"
fi

if command -v curl >/dev/null; then
    for port in "$CADDY_PORT" "$DIRECT_PORT"; do
        code="$(probe unknown-test.invalid "$port")"
        if [ "$code" = "421" ]; then
            echo "  Catch-all:   :${port} answers 421 (correct)"
        else
            warn "the catch-all of :${port} answered ${code} (expected 421)"
            verify_failed=1
        fi
    done
    # F9: every host Caddy served before this run, on the listener that serves it now, against
    # what it answered before anything changed.
    if [ -s "${WORK}/served" ]; then
        echo "  Hosts the configuration on disk served before this run (status of GET / with that Host: before -> now):"
        verify_hosts "${WORK}/served" || verify_failed=1
    fi
fi

echo ""
if [ "$verify_failed" = 1 ]; then
    echo "Setup finished, but the verification above found problems."
else
    echo "Setup complete."
fi
ON_STOP=""
echo ""
if [ -n "$DOMAIN" ]; then
    echo "cloudflared: send the hosts of ${DOMAIN} to Caddy with these ingress rules, in this order, and keep"
    echo "the original Host header (no httpHostHeader), which the guard signs. Direct sites bypass the"
    echo "gateway and go to their own listener, :${DIRECT_PORT}:"
    py ingress --domain "$DOMAIN" | sed 's/^/  /'
else
    # F36: the domain is recorded in the Caddyfile; a development host needs no /etc/webspec for it.
    echo "No public domain: Caddy serves *.localhost names only. To serve one, run this script again with"
    echo "WEBSPEC_DOMAIN=<domain> (the Caddyfile records it), then set WEBSPEC_DOMAIN=<domain> in"
    echo "${GATEWAY_SETTINGS} and restart the gateway, which reads it only when it starts:"
    echo "  ${RESTART_GATEWAY}"
fi
echo ""
allowed=""
for address in $EXPECTED_LISTENERS; do
    case "$address" in *":${CADDY_PORT}") allowed="${allowed:+${allowed} and }${address}" ;; esac
done
echo "DP-3: the agent's egress allow-list may name Caddy's gateway listener only: ${allowed}. The direct"
echo "listener, :${DIRECT_PORT}, bypasses the gateway, its guard and its audit log: keep it out."
echo ""
echo "After changing services, regenerate the site blocks as root. The Caddyfile records the public domain,"
echo "which webspec-ctl keeps unless WEBSPEC_DOMAIN says otherwise:"
printf '  %s caddy-sync\n' "$CTL"
echo "To change the public domain, give it once, then set it in ${GATEWAY_SETTINGS} too and restart"
echo "the gateway, which reads it only when it starts:"
printf '  %s caddy-sync\n' "${CTL/sudo /sudo WEBSPEC_DOMAIN=<domain> }"
echo "  ${RESTART_GATEWAY}"
if [ "$earlier_setup" = 1 ]; then
    echo ""
    warn "the Caddy that ran before this script logged without filters (DP-6). Its error entries in the journal (journalctl -u ${UNIT}) hold full request URIs, query strings (GET arguments) included, and the X-WebSpec-Guard and X-UFO-Clearance headers, and root and the members of the adm and systemd-journal groups can read them (getent group adm systemd-journal). journald cannot delete the entries of one unit: to drop them, rotate the journal and delete every entry older than now, of every unit (sudo journalctl --rotate && sudo journalctl --vacuum-time=1s), or make sure that no one else is in those groups."
fi
[ "$verify_failed" = 0 ] || exit 1
