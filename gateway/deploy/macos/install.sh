#!/bin/bash
#
# install.sh: install the WebSpec gateway on macOS as a LaunchDaemon that runs as the hidden
# system user _webspec (docs/spec/audit-deployment.md, DP-1 to DP-9).
#
#   sudo /opt/webspec/src/gateway/deploy/macos/install.sh
#   sudo WEBSPEC_DOMAIN=example.com /opt/webspec/src/gateway/deploy/macos/install.sh
#   gateway/deploy/macos/install.sh --dry-run      # print the plan; needs no root
#
# Root builds and installs the code of the checkout this script sits in, and runs the script
# from it. So run it from a checkout that only root can modify, at a commit you have reviewed;
# a real run refuses any other checkout unless ALLOW_NONROOT_SOURCE=1:
#   sudo /bin/mkdir -p /opt/webspec
#   sudo -H /usr/bin/git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
#   sudo -H /usr/bin/git -C /opt/webspec/src checkout --detach <reviewed commit>
# Run the script by its path, not as `sudo bash install.sh`, and type every command you give
# sudo by its absolute path: sudo looks a bare name up on your PATH, and the agent may be able
# to write a directory on it. Safest of all, do this from an admin account the agent never uses.
#
# Every run converges on this layout. Existing config.json, gateway.env, guard.key and
# allowed_signers are kept; owners and modes are re-applied, and ACLs removed.
#   _webspec:_webspec      hidden system user and group, free ID in 200-499, shell /usr/bin/false
#                          (an existing _webspec that looks like a login account, or shares an
#                          ID or a group with other accounts, is refused)
#   /opt/webspec/venv      root-owned virtualenv with this checkout's gateway/ installed
#   /etc/webspec           root:_webspec 0750
#     config.json          root:_webspec 0640, the MCP servers (created with none)
#     config.example.json  root:wheel 0644, the entry format (refreshed on every run)
#     gateway.env          root:_webspec 0640, WEBSPEC_DOMAIN and MCP-server secrets (shell syntax)
#     guard.key            root:_webspec 0440, created empty for you to fill; the gateway reads
#                          it through its group and cannot change it
#     allowed_signers      root:wheel 0644, OpenSSH allowed-signers for level-4 approvals
#   /var/lib/webspec       _webspec 0700, HOME and the audit log
#   /var/log/webspec       _webspec 0750, gateway.log
#   /Library/LaunchDaemons/com.webspec.gateway.daemon.plist, root:wheel 0644, installed unchanged;
#                          launchd holds the gateway's socket, 127.0.0.1:7002 (DP-9)
# The daemon is (re)started only when guard.key holds a usable key that the installed gateway
# can load and the other checks pass. Otherwise a daemon that is not loaded is disabled, so that
# launchd does not start it at the next boot either, until a run passes them. A loaded one is
# left running as it is, so that launchd keeps holding its port, unless gateway.env puts on PATH
# what another user can modify: then it is disabled and booted out. A run that leaves a loaded
# daemon unrestarted or stopped exits 1.
#
# Fill or rotate guard.key with --fill-key, the key piped in from your password manager:
#   /usr/local/bin/op read 'op://<vault>/<item>/<field>' |
#       sudo /opt/webspec/src/gateway/deploy/macos/install.sh --fill-key
# It replaces guard.key only with a key that the installed gateway accepts.
#
# Options: -n, --dry-run (the same as DRY_RUN=1); --fill-key; -h, --help.
# Environment. As root, only these variables pass; the rest of the environment is dropped.
#   DRY_RUN=1                print each command that would change the system instead of running
#                            it. Read-only probes (dscl, launchctl, find) still run.
#   WEBSPEC_DOMAIN=<domain>  public domain for Host routing, written into gateway.env (it is not
#                            a secret). Unset: gateway.env keeps its value. Empty: clears it.
#   PYTHON=<path>            Python 3.11+ to build the venv with (default: search known places).
#   ALLOW_NONROOT_PYTHON=1   accept a Python whose files a non-root user can modify.
#   ALLOW_NONROOT_SOURCE=1   accept a checkout whose files a non-root user can modify.
#   ALLOW_NONROOT_PATH=1     load the daemon although gateway.env puts on PATH a directory, or
#                            a command in one, that a non-root user can modify.
#   ALLOW_EXISTING_USER=1    adopt an existing _webspec that looks like a login account (a login
#                            shell, a UID of 500 or more, a home, shown at login, a password),
#                            whose group has other members, whose UID or GID is outside 200-499
#                            or another account's too (root's 0 included), or that is a member
#                            of other groups.
#
# Written for macOS 12 or newer and its /bin/bash (3.2).

set -euo pipefail

# As root, nothing in the invoking user's environment may steer what runs. sudo on macOS keeps
# that user's PATH (the default sudoers sets no secure_path) and HOME (env_keep), and sudo -E
# keeps everything, so a directory on that PATH, or a variable such as TMPDIR or SSL_CERT_FILE,
# may be the agent's. Before any other command runs, start over in a clean environment that
# carries only the variables listed above.
if [ "$EUID" -eq 0 ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
    for _name in $(compgen -e); do
        case $_name in
            PATH | HOME | PWD | OLDPWD | SHLVL | _ | DRY_RUN | WEBSPEC_DOMAIN | PYTHON | \
                ALLOW_NONROOT_PYTHON | ALLOW_NONROOT_SOURCE | ALLOW_NONROOT_PATH | ALLOW_EXISTING_USER) ;;
            *)
                exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=/var/root \
                    ${DRY_RUN+"DRY_RUN=$DRY_RUN"} \
                    ${WEBSPEC_DOMAIN+"WEBSPEC_DOMAIN=$WEBSPEC_DOMAIN"} \
                    ${PYTHON+"PYTHON=$PYTHON"} \
                    ${ALLOW_NONROOT_PYTHON+"ALLOW_NONROOT_PYTHON=$ALLOW_NONROOT_PYTHON"} \
                    ${ALLOW_NONROOT_SOURCE+"ALLOW_NONROOT_SOURCE=$ALLOW_NONROOT_SOURCE"} \
                    ${ALLOW_NONROOT_PATH+"ALLOW_NONROOT_PATH=$ALLOW_NONROOT_PATH"} \
                    ${ALLOW_EXISTING_USER+"ALLOW_EXISTING_USER=$ALLOW_EXISTING_USER"} \
                    /bin/bash "$0" "$@"
                ;;
        esac
    done
    PATH=/usr/bin:/bin:/usr/sbin:/sbin
    HOME=/var/root
    export PATH HOME
fi
umask 022

LABEL=com.webspec.gateway.daemon
DEV_LABEL=com.webspec.gateway
SVC_USER=_webspec
SVC_GROUP=_webspec
SVC_REALNAME="WebSpec Gateway"
ID_MIN=200
ID_MAX=499

PREFIX=/opt/webspec
VENV=/opt/webspec/venv
ETC=/etc/webspec
CONFIG=/etc/webspec/config.json
CONFIG_REF=/etc/webspec/config.example.json
ENV_FILE=/etc/webspec/gateway.env
GUARD_KEY=/etc/webspec/guard.key
SIGNERS=/etc/webspec/allowed_signers
STATE=/var/lib/webspec
LOGDIR=/var/log/webspec
PLIST_DST=/Library/LaunchDaemons/com.webspec.gateway.daemon.plist
PORT=7001
INTERNAL_PORT=7002
# The plist's Sockets entry, which launchd binds to 127.0.0.1:$INTERNAL_PORT and the gateway
# takes by this name (WEBSPEC_LAUNCHD_SOCKET).
SOCKET_NAME=gateway
REPO_URL=https://github.com/I-m-A-g-I-n-E/WebSpec.git
# What the plist runs, the PATH it gives the gateway, and its ExitTimeOut: the seconds launchd
# gives the gateway between SIGTERM and SIGKILL, longer than the 35 s the gateway takes at most to
# finish the requests in flight (webspec/__main__.py). check_plist holds the plist to them.
GATEWAY_CMD='set -ae; . /etc/webspec/gateway.env; exec /opt/webspec/venv/bin/python -I -m webspec'
SVC_PATH=/opt/webspec/venv/bin:/usr/bin:/bin:/usr/sbin:/sbin
EXIT_TIMEOUT=40

# The script's directory, without running dirname (see the clean environment above).
case ${BASH_SOURCE[0]} in
    */*) SCRIPT_DIR=${BASH_SOURCE[0]%/*} ;;
    *) SCRIPT_DIR=. ;;
esac
SCRIPT_DIR=$(CDPATH='' cd -P -- "${SCRIPT_DIR:-/}" && pwd)
REPO_ROOT=$(CDPATH='' cd -P -- "$SCRIPT_DIR/../../.." && pwd)
GATEWAY_SRC=$REPO_ROOT/gateway
PLIST_SRC=$SCRIPT_DIR/com.webspec.gateway.daemon.plist
CONFIG_EXAMPLE=$REPO_ROOT/gateway/deploy/config.example.json

DRY_RUN=${DRY_RUN:-0}
ALLOW_NONROOT_PYTHON=${ALLOW_NONROOT_PYTHON:-0}
ALLOW_NONROOT_SOURCE=${ALLOW_NONROOT_SOURCE:-0}
ALLOW_NONROOT_PATH=${ALLOW_NONROOT_PATH:-0}
ALLOW_EXISTING_USER=${ALLOW_EXISTING_USER:-0}
AS_ROOT=0
if [ "$EUID" -eq 0 ]; then AS_ROOT=1; fi
# Set, even to empty, means: write it into gateway.env.
DOMAIN_SET=0
DOMAIN=""
if [ -n "${WEBSPEC_DOMAIN+set}" ]; then
    DOMAIN_SET=1
    DOMAIN=$WEBSPEC_DOMAIN
fi
FILL_KEY=0
PY_REAL=""
PY_UNSAFE=""
DEV_AGENT="" # loaded development LaunchAgents, one domain/label per line
WORK=""
FILL_NEW="" # guard.key.new while --fill-key judges it
PLIST_SAME=0 # the installed plist was already this one
KEY_STATE=1  # key_state of guard.key: 0 usable, 1 empty, 2 not a usable key, 3 not read (dry run)
WAS_LOADED=0 # the job was loaded when load_daemon began
LOADED=0     # this run (re)started it
HELD=0       # it was loaded, and this run left it as it was, not restarted
STOPPED=0    # it was loaded, and this run booted it out
UNHEALTHY=0
FAILURE="" # why a real run exits 1
HEALTH_TRIES=40 # half-second waits for the gateway to take its socket
# Quarter-second waits for a bootout to finish: longer than launchd gives the gateway to stop.
UNLOAD_TRIES=$((4 * (EXIT_TIMEOUT + 10)))

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n== %s\n' "$*"; }
# The first reason, kept for the end of the run, why a real run exits 1.
fail() { if [ -z "$FAILURE" ]; then FAILURE=$*; fi; }

usage() {
    cat <<EOF
Usage: sudo $0 [--dry-run]
       <password manager> | sudo $0 --fill-key

Installs the WebSpec gateway as the LaunchDaemon $LABEL, running as $SVC_USER.

  -n, --dry-run  print the plan without changing anything (needs no root); same as DRY_RUN=1
  --fill-key     replace $GUARD_KEY with the key on standard input, piped in from your
                 password manager, if the installed gateway accepts it; nothing else changes
  -h, --help     show this help

Environment: WEBSPEC_DOMAIN, PYTHON, ALLOW_NONROOT_PYTHON, ALLOW_NONROOT_SOURCE,
ALLOW_NONROOT_PATH and ALLOW_EXISTING_USER, described at the top of this script.
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case $1 in
            -n | --dry-run) DRY_RUN=1 ;;
            --fill-key) FILL_KEY=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                printf '\ninstall.sh: unknown argument: %s\n' "$1" >&2
                exit 2
                ;;
        esac
        shift
    done
}

# Quote one word for display, so every printed command can be pasted back into a shell.
shquote() {
    case $1 in
        '') printf "''" ;;
        *[!A-Za-z0-9_./:=@%+,-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
        *) printf '%s' "$1" ;;
    esac
}

# Print a command that changes the system, then run it unless DRY_RUN=1.
run() {
    local line="" arg
    for arg in "$@"; do
        line="$line $(shquote "$arg")"
    done
    printf '+%s\n' "$line"
    if [ "$DRY_RUN" != 1 ]; then
        "$@"
    fi
}

# A private directory for files the installer writes before installing them (real runs only).
make_work() {
    if [ -z "$WORK" ]; then
        WORK=$(mktemp -d /tmp/webspec-install.XXXXXX)
        trap 'rm -rf "$WORK"' EXIT
    fi
}

# new_file <mode> <owner> <group> <dest>: install standard input as <dest>, which does not exist.
# A dry run prints the content instead.
new_file() {
    local name=${4##*/}
    if [ "$DRY_RUN" = 1 ]; then
        printf "+ cat > \"\$WORK/%s\" <<'EOF'\n" "$name"
        cat
        printf 'EOF\n'
        printf '+ install -m %s -o %s -g %s %s %s\n' "$1" "$2" "$3" "\"\$WORK/$name\"" "$(shquote "$4")"
        return 0
    fi
    make_work
    cat >"$WORK/$name"
    run install -m "$1" -o "$2" -g "$3" "$WORK/$name" "$4"
}

# keep <path> <owner> <group> <mode>: an existing file stays; its owner and mode are re-applied
# and any ACL is removed (chmod -N), which could grant more than the mode does. An ACL may date
# from when _webspec owned guard.key and could add one (F51).
keep() {
    say "keeping $1"
    run chown "$2:$3" "$1"
    run chmod "$4" "$1"
    run chmod -N "$1"
}

# True when the newline-separated list $2 contains the line $1.
has_line() {
    case "
$2
" in
        *"
$1
"*) return 0 ;;
    esac
    return 1
}

# dirname without running it (see the clean environment above).
dir_of() {
    case $1 in
        */*)
            set -- "${1%/*}"
            printf '%s\n' "${1:-/}"
            ;;
        *) printf '.\n' ;;
    esac
}

# Physical path of an existing file with every symlink followed (older macOS has no realpath).
resolve_path() {
    local p=$1 link dir n=0
    while [ -L "$p" ]; do
        n=$((n + 1))
        if [ "$n" -gt 40 ]; then return 1; fi
        link=$(readlink "$p") || return 1
        case $link in
            /*) p=$link ;;
            *) p=$(dir_of "$p")/$link ;;
        esac
    done
    if [ ! -e "$p" ]; then return 1; fi
    dir=$(CDPATH='' cd -P -- "$(dir_of "$p")" 2>/dev/null && pwd) || return 1
    printf '%s/%s\n' "${dir%/}" "${p##*/}"
}

describe() {
    stat -c '%U:%G %A' "$1" 2>/dev/null || stat -f '%Su:%Sg %Sp' "$1" 2>/dev/null || printf 'unknown owner'
}

# First path at or above $1 that someone other than root could modify, both as written (every
# component on the way; a symbolic link by its owner, since its mode bits mean nothing) and
# physically (where the links lead). A path that does not exist yet is judged by the existing
# directories above it, where it would be created.
unsafe_chain() {
    local d=$1 hit
    case $d in
        /*) ;;
        *) # relative: whoever chooses the working directory chooses the file
            printf '%s\n' "$d"
            return 0
            ;;
    esac
    while :; do
        if [ -L "$d" ]; then
            hit=$(find "$d" -maxdepth 0 ! -user root -print 2>/dev/null) || true
        else
            hit=$(find "$d" -maxdepth 0 \( ! -user root -o -perm -0020 -o -perm -0002 \) -print 2>/dev/null) || true
        fi
        if [ -n "$hit" ]; then
            printf '%s\n' "$hit"
            return 0
        fi
        if [ "$d" = / ]; then break; fi
        d=$(dir_of "$d")
    done
    d=$1
    if [ -e "$d" ] && [ ! -d "$d" ]; then
        d=$(resolve_path "$d") || {
            printf '%s\n' "$1"
            return 0
        }
        hit=$(find "$d" -maxdepth 0 \( ! -user root -o -perm -0020 -o -perm -0002 \) -print 2>/dev/null) || true
        if [ -n "$hit" ]; then
            printf '%s\n' "$hit"
            return 0
        fi
        d=$(dir_of "$d")
    fi
    while [ ! -d "$d" ]; do d=$(dir_of "$d"); done
    d=$(CDPATH='' cd -P -- "$d" 2>/dev/null && pwd) || {
        printf '%s\n' "$1"
        return 0
    }
    while :; do
        hit=$(find "$d" -maxdepth 0 \( ! -user root -o -perm -0020 -o -perm -0002 \) -print 2>/dev/null) || true
        if [ -n "$hit" ]; then
            printf '%s\n' "$hit"
            return 0
        fi
        if [ "$d" = / ]; then return 1; fi
        d=$(dir_of "$d")
    done
}

# First path below $1 that someone other than root could modify (a link given as $1 is followed;
# links inside are not, and their own mode bits mean nothing).
unsafe_tree() {
    local hit
    hit=$(find -H "$1" ! -type l \( ! -user root -o -perm -0020 -o -perm -0002 \) -print -quit 2>/dev/null) || true
    if [ -z "$hit" ]; then return 1; fi
    printf '%s\n' "$hit"
}

# What makes a path that an unsafe_* check found unsafe, for a message: its owner and mode, or,
# for a symbolic link that leads nowhere (its target is missing, or the links loop), that.
what_is() {
    if [ -L "$1" ] && [ ! -e "$1" ]; then
        printf 'a symbolic link to nothing\n'
    else
        describe "$1"
    fi
}

# $1 without a trailing / or /. (/ itself stays): written so, a symbolic link is followed before
# [ -L ] can see it, and a PATH entry means the same either way.
trim_path() {
    local p=$1
    while :; do
        case $p in
            ?*/) p=${p%/} ;;
            ?*/.) p=${p%/.} ;;
            *) break ;;
        esac
    done
    printf '%s\n' "$p"
}

# First path that someone other than root could modify on the way from $1 to the file it names:
# $1, then each symbolic link it leads through, hop by hop, every one judged by unsafe_chain (as
# written and physically). Judging only the final file would miss a root-owned link, or a link
# to a root-owned file, kept in a directory another user can write: that user can put their own
# file in its place. A link that leads nowhere is unsafe too (it is the path printed), as whoever
# can create its target decides what it names; so is $1 when its links go round in a loop (F47).
# Each hop is trimmed first (trim_path): /opt/webspec/bin/ would otherwise hide its links.
unsafe_links() {
    local p=$1 prev="" link dir n=0 hit
    while :; do
        p=$(trim_path "$p")
        if hit=$(unsafe_chain "$p"); then
            printf '%s\n' "$hit"
            return 0
        fi
        if [ ! -L "$p" ]; then
            if [ -n "$prev" ] && [ ! -e "$p" ]; then
                printf '%s\n' "$prev"
                return 0
            fi
            return 1
        fi
        n=$((n + 1))
        link=$(readlink "$p") || link=""
        if [ "$n" -gt 40 ] || [ -z "$link" ]; then
            printf '%s\n' "$1"
            return 0
        fi
        prev=$p
        case $link in
            /*) p=$link ;;
            *)
                dir=$(dir_of "$p")
                p=${dir%/}/$link
                ;;
        esac
    done
}

# The first entry of directory $1 that someone other than root could modify or replace, in
# words: a command found on PATH is one of these. A symbolic link among them is followed hop by
# hop (unsafe_links). $1 itself is followed if it is a link (find -H).
unsafe_entry() {
    local hit entry links
    hit=$(find -H "$1" -mindepth 1 -maxdepth 1 ! -type l \( ! -user root -o -perm -0020 -o -perm -0002 \) -print -quit 2>/dev/null) || true
    if [ -n "$hit" ]; then
        printf '%s is %s\n' "$hit" "$(what_is "$hit")"
        return 0
    fi
    links=$(find -H "$1" -mindepth 1 -maxdepth 1 -type l -print 2>/dev/null) || true
    while IFS= read -r entry; do
        if [ -z "$entry" ]; then continue; fi
        if hit=$(unsafe_links "$entry"); then
            if [ "$hit" = "$entry" ]; then
                printf '%s is %s\n' "$hit" "$(what_is "$hit")"
            else
                printf '%s leads through %s, which is %s\n' "$entry" "$hit" "$(what_is "$hit")"
            fi
            return 0
        fi
    done <<EOF
$links
EOF
    return 1
}

# The line numbers in a /bin/sh error, never its text, which can quote a secret. bash says
# "file: line 3: ...", dash (which /bin/sh can be) "file: 3: ...".
error_lines() {
    local lines
    lines=$(printf '%s\n' "$1" | grep -oE 'line [0-9]+|: [0-9]+: ' | sed -E 's/^: ([0-9]+): $/line \1/' |
        sort -u | tr '\n' ' ') || lines=""
    if [ -n "$lines" ]; then printf ' (%s)' "${lines% }"; fi
}

# ---- Source ------------------------------------------------------------------------------------
#
# Root builds this checkout's gateway/ with pip (its build hooks included), installs its plist as
# a LaunchDaemon and its example configuration, and runs this script from it. Whoever can modify
# any of that decides what root runs and what lands in the root-owned venv (DP-1, DP-4). Like the
# Python check below, this looks at owners and mode bits only (not ACLs): a heuristic, not a proof.

check_source() {
    local bad
    bad=$(unsafe_chain "$GATEWAY_SRC") || bad=$(unsafe_tree "$GATEWAY_SRC") || bad=""
    if [ -z "$bad" ]; then return 0; fi
    if [ "$ALLOW_NONROOT_SOURCE" = 1 ]; then
        warn "$bad ($(describe "$bad")) can be modified by a user other than root, who therefore decides"
        warn "what root builds and installs from $GATEWAY_SRC. Going ahead because ALLOW_NONROOT_SOURCE=1."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        warn "$bad ($(describe "$bad")) can be modified by a user other than root: a real run refuses"
        warn "to build and install from $GATEWAY_SRC unless ALLOW_NONROOT_SOURCE=1."
        return 0
    fi
    printf '%s\n' \
        "install.sh: refusing to install from $GATEWAY_SRC: $bad is $(describe "$bad")." \
        "" \
        "Root builds this checkout with pip (build hooks included), installs its plist as a LaunchDaemon" \
        "and runs this script from it, so whoever can modify it decides what root runs (DP-1, DP-4)." \
        "Install from a checkout that only root can modify, at a commit you have reviewed:" \
        "    sudo /bin/mkdir -p $PREFIX" \
        "    sudo -H /usr/bin/git clone $REPO_URL $PREFIX/src" \
        "    sudo -H /usr/bin/git -C $PREFIX/src checkout --detach <reviewed commit>" \
        "    sudo $PREFIX/src/gateway/deploy/macos/install.sh" \
        "Or set ALLOW_NONROOT_SOURCE=1 to accept the risk when the agent never runs as that user." >&2
    exit 1
}

# The plist becomes a root-owned LaunchDaemon. Before installing it, hold it to exactly the job
# this installer expects, comments aside: the same keys, user, program and environment, each
# value of the same type.
plist_value() { plutil -extract "$1" raw -o - "$PLIST_SRC" 2>/dev/null; }
# The type of the value at key path $1, as plutil names it: string, integer, bool, array...
# (-type came with macOS 12, as raw did: plutil(1), HISTORY).
plist_type() { plutil -type "$1" "$PLIST_SRC" 2>/dev/null; }

# The keys of the dictionary at key path $1 (empty for the top level), sorted, on one line.
plist_keys() {
    if [ -z "$1" ]; then
        plutil -convert xml1 -o - "$PLIST_SRC"
    else
        plutil -extract "$1" xml1 -o - "$PLIST_SRC"
    fi 2>/dev/null | awk -F '</?key>' '/^\t<key>/ { print $2 }' | LC_ALL=C sort | tr '\n' ' '
}

# Every value of the plist, one a line: its key path, its type (plutil's name) and its value as
# plutil prints it raw, where an array's value is its length. check_plist holds the plist to them.
plist_expected() {
    cat <<EOF
Label|string|$LABEL
UserName|string|$SVC_USER
GroupName|string|$SVC_GROUP
ProgramArguments|array|4
ProgramArguments.0|string|/bin/sh
ProgramArguments.1|string|-c
ProgramArguments.2|string|$GATEWAY_CMD
ProgramArguments.3|string|webspec-gateway
WorkingDirectory|string|$STATE
RunAtLoad|bool|true
KeepAlive|bool|true
ExitTimeOut|integer|$EXIT_TIMEOUT
ProcessType|string|Standard
Umask|integer|63
StandardOutPath|string|$LOGDIR/gateway.log
StandardErrorPath|string|$LOGDIR/gateway.log
EnvironmentVariables.HOME|string|$STATE
EnvironmentVariables.PATH|string|$SVC_PATH
EnvironmentVariables.WEBSPEC_CONFIG|string|$CONFIG
EnvironmentVariables.WEBSPEC_GUARD_KEY_FILE|string|$GUARD_KEY
EnvironmentVariables.WEBSPEC_AUDIT_LOG|string|$STATE/gateway-audit.jsonl
EnvironmentVariables.WEBSPEC_APPROVERS_FILE|string|$SIGNERS
EnvironmentVariables.WEBSPEC_SSH_KEYGEN|string|/usr/bin/ssh-keygen
EnvironmentVariables.WEBSPEC_LAUNCHD_SOCKET|string|$SOCKET_NAME
EnvironmentVariables.WEBSPEC_PORT|string|$PORT
EnvironmentVariables.WEBSPEC_INTERNAL_PORT|string|$INTERNAL_PORT
Sockets.$SOCKET_NAME.SockNodeName|string|127.0.0.1
Sockets.$SOCKET_NAME.SockServiceName|integer|$INTERNAL_PORT
Sockets.$SOCKET_NAME.SockFamily|string|IPv4
Sockets.$SOCKET_NAME.SockType|string|stream
EOF
}

check_plist() {
    local out key type want got
    if ! have plutil; then
        say "(no plutil on this system: $PLIST_SRC is not checked)"
        return 0
    fi
    out=$(plutil -lint "$PLIST_SRC" 2>&1) || die "$out"
    want="EnvironmentVariables ExitTimeOut GroupName KeepAlive Label ProcessType ProgramArguments"
    want="$want RunAtLoad Sockets StandardErrorPath StandardOutPath Umask UserName WorkingDirectory "
    got=$(plist_keys "")
    if [ "$got" != "$want" ]; then die "$PLIST_SRC has the keys: ${got% }; expected: ${want% }"; fi
    want="HOME PATH WEBSPEC_APPROVERS_FILE WEBSPEC_AUDIT_LOG WEBSPEC_CONFIG WEBSPEC_GUARD_KEY_FILE"
    want="$want WEBSPEC_INTERNAL_PORT WEBSPEC_LAUNCHD_SOCKET WEBSPEC_PORT WEBSPEC_SSH_KEYGEN "
    got=$(plist_keys EnvironmentVariables)
    if [ "$got" != "$want" ]; then die "$PLIST_SRC sets the variables: ${got% }; expected: ${want% }"; fi
    # DP-8, DP-9: one socket, which launchd holds on loopback for the gateway.
    want="$SOCKET_NAME "
    got=$(plist_keys Sockets)
    if [ "$got" != "$want" ]; then die "$PLIST_SRC has the sockets: ${got% }; expected: ${want% }"; fi
    want="SockFamily SockNodeName SockServiceName SockType "
    got=$(plist_keys "Sockets.$SOCKET_NAME")
    if [ "$got" != "$want" ]; then die "$PLIST_SRC: Sockets.$SOCKET_NAME has the keys: ${got% }; expected: ${want% }"; fi
    # Each value's type, then the value: plutil prints <string>40</string> as it prints
    # <integer>40</integer>, and launchd.plist(5) gives each key a type (ExitTimeOut <integer>,
    # RunAtLoad <boolean>).
    while IFS='|' read -r key type want; do
        got=$(plist_type "$key") || got='<absent>'
        if [ "$got" != "$type" ]; then die "$PLIST_SRC: $key is of type '$got', expected '$type'"; fi
        got=$(plist_value "$key") || got='<absent>'
        if [ "$got" != "$want" ]; then die "$PLIST_SRC: $key is '$got', expected '$want'"; fi
    done <<EOF
$(plist_expected)
EOF
}

# ---- Python ------------------------------------------------------------------------------------
#
# The gateway's interpreter and standard library must be as safe from the agent as its code:
# whoever can modify them runs code as _webspec and can read the guard key, which undoes DP-1
# and DP-4. Homebrew belongs to the user who installed it (usually the agent's user), and the
# python.org installer makes its framework writable by the admin group. A real run therefore
# accepts only an interpreter that root alone can modify, unless ALLOW_NONROOT_PYTHON=1, and
# without that override no run (dry or real) executes such an interpreter as root. The check
# covers the executable, every directory above it, and the installation prefix
# (<prefix>/bin/python3.X); it is a heuristic for these layouts, not a proof.

# 3.12 and 3.11 first, which every CI matrix in .github/workflows/tests.yml has tested, then newer ones.
python_candidates() {
    local v d
    if [ -n "${PYTHON:-}" ]; then
        printf '%s\n' "$PYTHON"
        return 0
    fi
    for v in 3.12 3.11 3.13 3.14; do
        for d in /opt/local/bin "/Library/Frameworks/Python.framework/Versions/$v/bin" /opt/homebrew/bin /usr/local/bin; do
            printf '%s\n' "$d/python$v"
        done
    done
    printf '%s\n' /opt/homebrew/bin/python3 /usr/local/bin/python3
}

python_ok() {
    "$1" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

select_python() {
    local list cand real bad rejected=""
    list=$(python_candidates)
    # Pass 1: an interpreter only root can modify, checked before it is ever executed.
    while IFS= read -r cand; do
        if [ -z "$cand" ] || [ ! -x "$cand" ]; then continue; fi
        real=$(resolve_path "$cand") || continue
        if unsafe_chain "$real" >/dev/null; then continue; fi
        if unsafe_tree "$(dir_of "$(dir_of "$real")")" >/dev/null; then continue; fi
        if ! python_ok "$real"; then continue; fi
        PY_REAL=$real
        return 0
    done <<EOF
$list
EOF
    # Pass 2: anything else is refused by a real run unless ALLOW_NONROOT_PYTHON=1.
    while IFS= read -r cand; do
        if [ -z "$cand" ] || [ ! -x "$cand" ]; then continue; fi
        real=$(resolve_path "$cand") || continue
        bad=$(unsafe_chain "$real") || bad=$(unsafe_tree "$(dir_of "$(dir_of "$real")")") || bad=""
        if [ -z "$bad" ]; then continue; fi # root-owned but too old; pass 1 saw it
        if [ "$ALLOW_NONROOT_PYTHON" != 1 ]; then
            if [ "$DRY_RUN" != 1 ]; then
                rejected="$rejected
  $cand: $bad is $(describe "$bad")"
                continue
            fi
            if [ "$AS_ROOT" = 1 ]; then
                # A dry run as root shows the plan with it but does not execute it.
                PY_REAL=$real
                PY_UNSAFE=$bad
                return 0
            fi
        fi
        if ! python_ok "$real"; then continue; fi
        PY_REAL=$real
        PY_UNSAFE=$bad
        return 0
    done <<EOF
$list
EOF
    if [ -n "$rejected" ]; then
        printf '%s\n' \
            "install.sh: no root-owned Python 3.11+ found. A non-root user can modify these:$rejected" \
            "" \
            "Whoever can modify the gateway's interpreter can run code as $SVC_USER and read the guard" \
            "key (DP-1, DP-4). Use a Python only root can modify, and name it with PYTHON=, e.g.:" \
            "  python.org installer, then remove the admin group's write access it grants:" \
            "    sudo /bin/chmod -R go-w /Library/Frameworks/Python.framework" \
            "    sudo PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 $0" \
            "  MacPorts (root-owned /opt/local): sudo /opt/local/bin/port install python312" \
            "Or set ALLOW_NONROOT_PYTHON=1 to accept the risk when the agent never runs as that user." >&2
        exit 1
    fi
    if [ -n "${PYTHON:-}" ]; then
        die "PYTHON=$PYTHON is not an executable Python 3.11 or newer"
    fi
    die "no Python 3.11 or newer found. Install one (python.org or MacPorts) or name it with PYTHON=."
}

# ---- Preflight (read-only) ---------------------------------------------------------------------

preflight() {
    local f p re
    if [ "$DRY_RUN" = 1 ]; then
        say "DRY RUN: commands that would change the system are printed, not run."
    elif [ "$DRY_RUN" != 0 ]; then
        die "DRY_RUN must be 0 or 1"
    elif [ "$AS_ROOT" != 1 ]; then
        die "must be run as root: sudo $0 (or $0 --dry-run to print the plan)"
    elif [ "$(uname -s)" != Darwin ]; then
        die "this installer is for macOS; see gateway/deploy/ for other platforms"
    fi
    cd /

    for f in "$GATEWAY_SRC/pyproject.toml" "$PLIST_SRC" "$CONFIG_EXAMPLE"; do
        if [ ! -f "$f" ]; then die "missing $f (run this script from a WebSpec checkout)"; fi
    done
    # chown and chmod follow symbolic links: never let one redirect them.
    for p in "$PREFIX" "$VENV" "$ETC" "$CONFIG" "$CONFIG_REF" "$ENV_FILE" "$GUARD_KEY" "$SIGNERS" \
        "$STATE" "$LOGDIR" "$PLIST_DST"; do
        if [ -L "$p" ]; then die "$p is a symbolic link; this installer manages only real files and directories"; fi
    done
    case ${PYTHON:-/} in
        /*) ;;
        *) die "PYTHON=$PYTHON must be an absolute path" ;;
    esac
    re='^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
    if [ -n "$DOMAIN" ] && ! [[ $DOMAIN =~ $re ]]; then
        die "WEBSPEC_DOMAIN=$DOMAIN is not a lowercase DNS name such as example.com"
    fi

    check_source
    check_plist
    select_python
}

# A loaded development LaunchAgent (gateway/launchd) runs a second gateway as a login user, the
# agent's as a rule, on port 7001, which Caddy needs (DP-1, DP-7). Every login user (UID 500 and
# up) is checked, not only the one who ran sudo: the safest setup runs this installer from an
# admin account the agent never uses (F48). Without root, a dry run sees only its own user's.
check_dev_agent() {
    local users="" user uid domain home plist seen=""
    if ! have launchctl || ! have dscl; then return 0; fi
    users=$(dscl . -list /Users UniqueID 2>/dev/null |
        awk 'NF >= 2 && $NF ~ /^[0-9]+$/ && $NF + 0 >= 500 { print $1, $NF }') || users=""
    # The list arrives on descriptor 3, so that nothing in the loop can read it from stdin.
    while read -r user uid <&3; do
        if [ -z "$uid" ]; then continue; fi
        for domain in "gui/$uid" "user/$uid"; do
            if launchctl print "$domain/$DEV_LABEL" >/dev/null 2>&1; then
                DEV_AGENT="$DEV_AGENT${DEV_AGENT:+
}$domain/$DEV_LABEL"
                warn "the development LaunchAgent $DEV_LABEL is loaded for $user ($domain)."
                warn "It runs a second gateway as $user on port $PORT. Boot it out first, as $user (not root),"
                warn "then run this installer again:"
                warn "    /bin/launchctl bootout $domain/$DEV_LABEL"
                warn "    /bin/launchctl disable $domain/$DEV_LABEL"
                warn "Until then this run installs everything but does not (re)start $LABEL."
                break
            fi
        done
        # Not loaded now, but loaded at the user's next login unless disabled.
        home=$(ds_values "/Users/$user" NFSHomeDirectory | head -n 1) || home=""
        for plist in "${home:-/nonexistent}/Library/LaunchAgents/$DEV_LABEL.plist" "/Users/$user/Library/LaunchAgents/$DEV_LABEL.plist"; do
            if [ -e "$plist" ] && ! has_line "$plist" "$seen"; then
                seen="$seen
$plist"
                warn "$plist loads the development gateway at $user's next login unless it is"
                warn "disabled: /bin/launchctl disable gui/$uid/$DEV_LABEL (as $user)."
            fi
        done
    done 3<<EOF
$users
EOF
    for plist in /Library/LaunchAgents/"$DEV_LABEL".plist /Users/*/Library/LaunchAgents/"$DEV_LABEL".plist; do
        if [ -e "$plist" ] && ! has_line "$plist" "$seen"; then
            seen="$seen
$plist"
            warn "$plist loads the development gateway at a login unless it is disabled for that user."
        fi
    done
}

# ---- Service account ---------------------------------------------------------------------------
#
# dscl creates a record and its attributes in separate steps, so an interrupted run can leave a
# record without its ID. Each step below checks what exists and fills in only what is missing.

record_exists() { dscl . -read "$1" RecordName >/dev/null 2>&1; }

# First value of attribute $2 of record $1; fails when either is missing. (dscl -read prints
# "No such key" and exits 0 for a missing attribute, so the awk match decides.)
ds_value() {
    local out
    out=$(dscl . -read "$1" "$2" 2>/dev/null) || return 1
    awk -v key="$2:" '$1 == key && NF >= 2 { print $2; found = 1; exit } END { exit !found }' <<<"$out"
}

# Every value of attribute $2 of record $1, one per line; fails when either is missing. dscl
# prints "Key: v1 v2", or "Key:" and then one line per value, indented, when a value holds a space.
ds_values() {
    local out
    out=$(dscl . -read "$1" "$2" 2>/dev/null) || return 1
    awk -v key="$2:" '
        !found && $1 == key { found = 1; for (i = 2; i <= NF; i++) print $i; next }
        found && /^ / { sub(/^ /, ""); print; next }
        found { exit }
        END { exit !found }
    ' <<<"$out"
}

# The other records of directory $2 (Users or Groups) whose attribute $3 is $1, on one line; fails
# when there is none. $4 is the record that may have it.
others_with_id() { # others_with_id <id> Users UniqueID | Groups PrimaryGroupID <own name>
    local v
    v=$(dscl . -list "/$2" "$3" 2>/dev/null |
        awk -v id="$1" -v me="$4" 'NF >= 2 && $NF == id && $1 != me { print $1 }' | tr '\n' ' ') || v=""
    if [ -z "$v" ]; then return 1; fi
    printf '%s\n' "${v% }"
}

# DP-1: what makes an existing _webspec unlike the hidden service account this installer makes,
# one reason per line (nothing: it may be adopted). Someone who can log in as it, or who is in its
# group, can read guard.key and gateway.env. converge_account takes away the shell, the home and
# the login-window entry on every run, but not a password or the group's other members, so the
# first run must refuse what it cannot undo (F15). Nor can it undo IDs that another account
# shares, root's 0 included: files of one are files of the other, and the gateway would run as
# that account. Nor memberships in other groups, whose IDs launchd gives the gateway and its
# stdio servers too (InitGroups). Attributes that are missing, as an interrupted run leaves
# them, are no reason.
account_problems() { # account_problems <uid> <gid>
    local v me_guid
    case $1 in
        '') ;;
        *[!0-9]*) printf 'its UID is %s\n' "$1" ;;
        *)
            if [ "$1" -ge 500 ]; then
                printf 'its UID, %s, is in the range of login users (500 and up)\n' "$1"
            elif [ "$1" -lt "$ID_MIN" ]; then
                printf 'its UID, %s, is below %s: this installer gives service accounts %s-%s\n' "$1" "$ID_MIN" "$ID_MIN" "$ID_MAX"
            fi
            if v=$(others_with_id "$1" Users UniqueID "$SVC_USER"); then
                printf 'its UID, %s, is also the UID of %s\n' "$1" "$v"
            fi
            ;;
    esac
    case $2 in
        '') ;;
        *[!0-9]*) printf "its group's GID is %s\n" "$2" ;;
        *)
            if [ "$2" -lt "$ID_MIN" ] || [ "$2" -gt "$ID_MAX" ]; then
                printf "its group's GID, %s, is outside %s-%s, where this installer makes it\n" "$2" "$ID_MIN" "$ID_MAX"
            fi
            if v=$(others_with_id "$2" Groups PrimaryGroupID "$SVC_GROUP"); then
                printf "its group's GID, %s, is also the GID of the group %s\n" "$2" "$v"
            fi
            ;;
    esac
    if v=$(ds_values "/Users/$SVC_USER" UserShell); then
        case $v in
            '' | /usr/bin/false | /bin/false | /usr/sbin/nologin | /sbin/nologin) ;;
            *) printf 'its login shell is %s\n' "$v" ;;
        esac
    fi
    if v=$(ds_values "/Users/$SVC_USER" NFSHomeDirectory); then
        case $v in
            '' | /var/empty | "$STATE") ;;
            *) printf 'its home is %s\n' "$v" ;;
        esac
    fi
    if v=$(ds_values "/Users/$SVC_USER" IsHidden); then
        case $v in
            1 | true | TRUE | yes | YES) ;;
            *) printf 'it is shown at login (IsHidden %s)\n' "$v" ;;
        esac
    fi
    if v=$(ds_values "/Users/$SVC_USER" AuthenticationAuthority); then
        case $v in
            *ShadowHash* | *Kerberos*) printf 'it has a password (AuthenticationAuthority)\n' ;;
        esac
    fi
    # The group: its listed members, by name or by GUID, nested groups, and other users whose
    # primary group it is.
    me_guid=$(ds_value "/Users/$SVC_USER" GeneratedUID) || me_guid=""
    if v=$(ds_values "/Groups/$SVC_GROUP" GroupMembership); then
        v=$(printf '%s\n' "$v" | awk -v me="$SVC_USER" '$0 != me && $0 != ""' | tr '\n' ' ')
        if [ -n "$v" ]; then printf 'its group %s has the members %s\n' "$SVC_GROUP" "${v% }"; fi
    fi
    if v=$(ds_values "/Groups/$SVC_GROUP" GroupMembers); then
        v=$(printf '%s\n' "$v" | awk -v me="$me_guid" '$0 != me && $0 != ""' | tr '\n' ' ')
        if [ -n "$v" ]; then printf 'its group %s has the members %s (GUIDs)\n' "$SVC_GROUP" "${v% }"; fi
    fi
    if v=$(ds_values "/Groups/$SVC_GROUP" NestedGroups) && [ -n "$v" ]; then
        printf 'its group %s has nested groups\n' "$SVC_GROUP"
    fi
    if [ -n "$2" ]; then
        v=$(dscl . -list /Users PrimaryGroupID 2>/dev/null |
            awk -v g="$2" -v me="$SVC_USER" 'NF >= 2 && $NF == g && $1 != me { print $1 }' | tr '\n' ' ') || v=""
        if [ -n "$v" ]; then printf 'its group %s is the primary group of %s\n' "$SVC_GROUP" "${v% }"; fi
    fi
    # Other groups that list it, by name or by GUID (dscl lists only the groups that have members).
    v=$({
        dscl . -list /Groups GroupMembership 2>/dev/null | member_of "$SVC_USER" || true
        if [ -n "$me_guid" ]; then dscl . -list /Groups GroupMembers 2>/dev/null | member_of "$me_guid" || true; fi
    } | awk '!seen[$0]++' | tr '\n' ' ')
    if [ -n "$v" ]; then printf 'it is a member of the groups %s\n' "${v% }"; fi
    return 0
}

# The groups, other than $SVC_GROUP, whose members in a `dscl . -list /Groups <attribute>` listing
# on standard input include $1.
member_of() {
    awk -v me="$1" -v own="$SVC_GROUP" '$1 != own { for (i = 2; i <= NF; i++) if ($i == me) { print $1; break } }'
}

check_account() { # check_account <uid> <gid>
    local problems line
    problems=$(account_problems "$1" "$2")
    if [ -z "$problems" ]; then return 0; fi
    while IFS= read -r line; do
        warn "existing $SVC_USER: $line"
    done <<EOF
$problems
EOF
    if [ "$ALLOW_EXISTING_USER" = 1 ]; then
        warn "Adopting it anyway because ALLOW_EXISTING_USER=1. This run removes its login shell, its home"
        warn "and its login-window entry, but not a password, the group's other members, who can read"
        warn "$GUARD_KEY and $ENV_FILE, IDs it shares with other accounts, or its other groups."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        warn "A real run refuses to adopt this $SVC_USER unless ALLOW_EXISTING_USER=1."
        return 0
    fi
    printf '%s\n' \
        "install.sh: refusing to adopt the existing $SVC_USER: it does not look like a hidden service account." \
        "" \
        "The gateway runs as $SVC_USER, which can read the guard key and gateway.env (DP-1): no person, and" \
        "not the agent, may be able to log in as it or share its group or its IDs. Delete it, or rename" \
        "it, and run this installer again to create a fresh one, for example:" \
        "    sudo /usr/bin/dscl . -delete /Users/$SVC_USER" \
        "    sudo /usr/bin/dscl . -delete /Groups/$SVC_GROUP" \
        "Groups list their members by name, so take it out of every other group it is in first:" \
        "    sudo /usr/sbin/dseditgroup -o edit -d $SVC_USER -t user <group>" \
        "Or set ALLOW_EXISTING_USER=1 to adopt it as it is (see the warnings above)." >&2
    exit 1
}

# True when no record of directory $2, other than the record $4, has $1 as its attribute $3. IDs
# compare as numbers, as in account_problems (0450 is 450); if dscl fails, $1 counts as used
# (pipefail).
id_unused() { # id_unused <id> Users UniqueID | Groups PrimaryGroupID | Users PrimaryGroupID [<own name>]
    dscl . -list "/$2" "$3" |
        awk -v id="$1" -v me="${4:-}" 'NF >= 2 && $NF == id && $1 != me { found = 1 } END { exit found }'
}

# True when $1 is a number in [ID_MIN, ID_MAX], the range this installer gives IDs from, written
# as id prints it (no leading zero): ensure_account checks the UID it gives against id -u.
id_in_range() {
    case $1 in
        '' | *[!0-9]* | 0?*) return 1 ;;
    esac
    [ "$1" -ge "$ID_MIN" ] && [ "$1" -le "$ID_MAX" ]
}

# Highest ID in [ID_MIN, ID_MAX] that no local user or group uses, as its own ID or as a user's
# primary group, read as numbers: a group with a user's primary GID has that user in it, and
# account_problems refuses it. Apple fills this range from the bottom (macOS 27 uses up to 308,
# plus a few in the 400s), so counting down from 499 keeps clear of the accounts future
# releases add.
free_id() {
    local used i=$ID_MAX
    used=$({ dscl . -list /Users UniqueID && dscl . -list /Groups PrimaryGroupID && dscl . -list /Users PrimaryGroupID; } |
        awk 'NF >= 2 { print $NF + 0 }') || return 1
    while [ "$i" -ge "$ID_MIN" ]; do
        if ! has_line "$i" "$used"; then
            printf '%s\n' "$i"
            return 0
        fi
        i=$((i - 1))
    done
    return 1
}

ensure_account() {
    local uid="" gid="" ugid="" user_exists=0 group_exists=0
    section "Service account $SVC_USER:$SVC_GROUP"
    if ! have dscl; then
        if [ "$DRY_RUN" != 1 ]; then die "dscl not found"; fi
        say "(no dscl on this system: the plan shows a new account with a placeholder ID)"
        gid="<free-id>"
        uid="<free-id>"
        run dscl . -create "/Groups/$SVC_GROUP"
        run dscl . -create "/Groups/$SVC_GROUP" PrimaryGroupID "$gid"
        run dscl . -create "/Users/$SVC_USER"
        run dscl . -create "/Users/$SVC_USER" UniqueID "$uid"
        run dscl . -create "/Users/$SVC_USER" PrimaryGroupID "$gid"
        converge_account
        return 0
    fi
    if record_exists "/Users/$SVC_USER"; then
        user_exists=1
        uid=$(ds_value "/Users/$SVC_USER" UniqueID) || uid=""
        ugid=$(ds_value "/Users/$SVC_USER" PrimaryGroupID) || ugid=""
    fi
    if record_exists "/Groups/$SVC_GROUP"; then
        group_exists=1
        gid=$(ds_value "/Groups/$SVC_GROUP" PrimaryGroupID) || gid=""
    fi
    if [ "$user_exists" = 1 ] || [ "$group_exists" = 1 ]; then
        check_account "$uid" "$gid"
    fi

    if [ -n "$gid" ]; then
        say "group $SVC_GROUP exists (GID $gid)"
    else
        # Prefer the GID the user already names, if the next run accepts it for the group: in
        # ID_MIN-ID_MAX, no other group's and no other user's primary group (account_problems).
        # Otherwise a free one, and the user's primary group is moved to it below.
        if id_in_range "$ugid" && id_unused "$ugid" Groups PrimaryGroupID &&
            id_unused "$ugid" Users PrimaryGroupID "$SVC_USER"; then
            gid=$ugid
        else
            gid=$(free_id) || die "no free ID in $ID_MIN-$ID_MAX for group $SVC_GROUP"
            if [ -n "$ugid" ]; then
                say "$SVC_USER's primary GID, $ugid, is not a free ID in $ID_MIN-$ID_MAX: giving group $SVC_GROUP $gid instead"
            fi
        fi
        if [ "$group_exists" = 1 ]; then
            say "group $SVC_GROUP exists without a PrimaryGroupID (an interrupted run?): giving it $gid"
        else
            run dscl . -create "/Groups/$SVC_GROUP"
        fi
        run dscl . -create "/Groups/$SVC_GROUP" PrimaryGroupID "$gid"
    fi

    if [ -n "$uid" ]; then
        say "user $SVC_USER exists (UID $uid)"
    else
        # The group's number, if the next run accepts it as the UID: in ID_MIN-ID_MAX (a group
        # adopted with ALLOW_EXISTING_USER=1 may be outside it) and no other user's.
        if id_in_range "$gid" && id_unused "$gid" Users UniqueID; then
            uid=$gid
        else
            uid=$(free_id) || die "no free ID in $ID_MIN-$ID_MAX for user $SVC_USER"
        fi
        if [ "$user_exists" = 1 ]; then
            say "user $SVC_USER exists without a UniqueID (an interrupted run?): giving it $uid"
        else
            run dscl . -create "/Users/$SVC_USER"
        fi
        run dscl . -create "/Users/$SVC_USER" UniqueID "$uid"
    fi
    if [ -z "$ugid" ]; then
        run dscl . -create "/Users/$SVC_USER" PrimaryGroupID "$gid"
    elif [ "$ugid" != "$gid" ]; then
        # launchd runs the gateway as $SVC_GROUP (GroupName), but check_guard_key's probe gets the
        # user's own groups, and guard.key is readable through $SVC_GROUP only (F51).
        say "$SVC_USER's primary group is $ugid, not $SVC_GROUP ($gid): making it $SVC_GROUP"
        run dscl . -create "/Users/$SVC_USER" PrimaryGroupID "$gid"
        run dscacheutil -flushcache
    fi
    converge_account
    if [ "$DRY_RUN" != 1 ] && [ "$(id -u "$SVC_USER" 2>/dev/null)" != "$uid" ]; then
        run dscacheutil -flushcache
        [ "$(id -u "$SVC_USER" 2>/dev/null)" = "$uid" ] || die "the system does not resolve $SVC_USER to UID $uid"
    fi
}

# Re-applied on every run: no password, no login shell, no home of its own, hidden from the
# login window.
converge_account() {
    run dscl . -create "/Groups/$SVC_GROUP" RealName "$SVC_REALNAME"
    run dscl . -create "/Groups/$SVC_GROUP" Password '*'
    run dscl . -create "/Users/$SVC_USER" RealName "$SVC_REALNAME"
    run dscl . -create "/Users/$SVC_USER" Password '*'
    run dscl . -create "/Users/$SVC_USER" UserShell /usr/bin/false
    run dscl . -create "/Users/$SVC_USER" NFSHomeDirectory /var/empty
    run dscl . -create "/Users/$SVC_USER" IsHidden 1
}

# ---- Files -------------------------------------------------------------------------------------

ensure_dir() { # ensure_dir <dir> <owner> <group> <mode>
    run mkdir -p "$1"
    run chown "$2:$3" "$1"
    run chmod "$4" "$1"
}

# WEBSPEC_DOMAIN=$DOMAIN in the existing gateway.env, its other lines untouched. The file holds
# secrets, so it is rewritten in the private work directory and never printed.
set_env_domain() {
    if [ "$DRY_RUN" = 1 ]; then
        say "# set WEBSPEC_DOMAIN=$DOMAIN in $ENV_FILE, keeping its other lines"
        printf '+ install -m 0640 -o root -g %s %s %s\n' "$SVC_GROUP" "\"\$WORK/gateway.env\"" "$ENV_FILE"
        return 0
    fi
    make_work
    awk -v d="$DOMAIN" '
        /^[[:space:]]*(export[[:space:]]+)?WEBSPEC_DOMAIN=/ { if (!done) print "WEBSPEC_DOMAIN=" d; done = 1; next }
        { print }
        END { if (!done) print "WEBSPEC_DOMAIN=" d }
    ' "$ENV_FILE" >"$WORK/gateway.env"
    if cmp -s "$WORK/gateway.env" "$ENV_FILE"; then
        say "WEBSPEC_DOMAIN in $ENV_FILE is already '$DOMAIN'"
        return 0
    fi
    run install -m 0640 -o root -g "$SVC_GROUP" "$WORK/gateway.env" "$ENV_FILE"
}

install_files() {
    section "Configuration $ETC (DP-4: root-owned, unreadable to the agent's user)"
    if [ "$DRY_RUN" = 1 ] && [ -d "$ETC" ] && [ ! -x "$ETC" ]; then
        say "(without root $ETC cannot be read, so files already in it show as new)"
    fi
    ensure_dir "$ETC" root "$SVC_GROUP" 0750

    # Not the example itself, whose servers would go live: a configuration with none.
    if [ -e "$CONFIG" ]; then
        keep "$CONFIG" root "$SVC_GROUP" 0640
    else
        new_file 0640 root "$SVC_GROUP" "$CONFIG" <<'EOF'
{
  "_comment": [
    "WebSpec gateway configuration (WEBSPEC_CONFIG), root:_webspec 0640. The gateway re-reads it every 30 seconds.",
    "Add MCP servers under mcpServers; config.example.json next to this file shows the format (docs/spec/audit-deployment.md, Configuration).",
    "Secrets: put NAME='value' in /etc/webspec/gateway.env and write \"${NAME}\" here, in a stdio server's env or an http server's headers. Never in args: any local user can read a process's arguments.",
    "A stdio server runs as _webspec, which can read the guard key: give it an absolute command, and keep its program and code root-owned and outside every user's home, for example under /opt/webspec/services.",
    "Edit this file as root with a fixed editor (sudo -H /usr/bin/vi /etc/webspec/config.json), never with sudo -e."
  ],
  "mcpServers": {}
}
EOF
    fi
    run install -m 0644 -o root -g wheel "$CONFIG_EXAMPLE" "$CONFIG_REF"

    if [ -e "$ENV_FILE" ]; then
        keep "$ENV_FILE" root "$SVC_GROUP" 0640
        if [ "$DOMAIN_SET" = 1 ]; then set_env_domain; fi
    else
        new_file 0640 root "$SVC_GROUP" "$ENV_FILE" <<EOF
# $ENV_FILE: site settings and MCP-server secrets of the WebSpec gateway
# (root:$SVC_GROUP 0640). The LaunchDaemon's /bin/sh runs \`set -ae; . $ENV_FILE\`
# as $SVC_USER before it starts the gateway: every assignment becomes part of the gateway's
# environment and overrides the plist's, and a failing line stops the gateway from starting.
# install.sh never overwrites this file.
#
# Shell syntax, one NAME='value' per line. Single quotes keep spaces and \$ \` \\ " literal
# (write a single quote as '\\''). The stdio servers run as $SVC_USER and can read this file;
# the agent's user cannot.
#
# Edit it as root with a fixed editor: sudo -H /usr/bin/vi $ENV_FILE
# Never with sudo -e (sudoedit), which hands a copy to your own user, the one the agent may share.
# Then run install.sh again, which checks this file before it restarts the gateway, or restart it
# without the checks: sudo /bin/launchctl kickstart -k system/$LABEL

# Public domain that Caddy forwards to the gateway, such as i-a-m.live. Empty: the gateway
# serves loopback names (*.localhost) only.
WEBSPEC_DOMAIN=$DOMAIN

# MCP-server secrets. config.json refers to them as "\${NAME}" in a stdio server's "env" or an
# http server's "headers", never in "args": any local user can read a process's arguments.
#TICKETS_TOKEN='...'

# More directories for the stdio servers' commands (npx, node, op): append only, and only
# directories whose commands root alone can modify. Homebrew's belong to the user who installed
# it, and /usr/local/bin often links into apps that a user owns. A directory of your own is
# simplest:
#   sudo /bin/mkdir -p /opt/webspec/bin && sudo /bin/ln -s /usr/local/bin/op /opt/webspec/bin/
#PATH="\$PATH:/opt/webspec/bin"

# Not here: the guard key. It lives in $GUARD_KEY (WEBSPEC_GUARD_KEY_FILE).
EOF
    fi

    # root-owned, read by the gateway through its group: the owner of a file can chmod it and
    # rewrite it whatever the directory allows, and every stdio server runs as $SVC_USER (F51).
    if [ -e "$GUARD_KEY" ]; then
        keep "$GUARD_KEY" root "$SVC_GROUP" 0440
    else
        run install -m 0440 -o root -g "$SVC_GROUP" /dev/null "$GUARD_KEY"
    fi

    if [ -e "$SIGNERS" ]; then
        keep "$SIGNERS" root wheel 0644
    else
        new_file 0644 root wheel "$SIGNERS" <<'EOF'
# /etc/webspec/allowed_signers: the people who may approve level-4 requests, in OpenSSH
# allowed-signers format (docs/spec/levels.md, AP-3). root:wheel 0644: the gateway holds public
# keys only. Until this file names someone, requests that need an approval are refused with
# 503 approval_unavailable (AP-7). One line per approver:
#   ana@example.com namespaces="webspec-approval" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
# Edit it as root with a fixed editor (sudo -H /usr/bin/vi /etc/webspec/allowed_signers), never
# with sudo -e: whoever can add a line here can approve level-4 requests.
EOF
    fi

    section "State $STATE and logs $LOGDIR"
    ensure_dir "$STATE" "$SVC_USER" "$SVC_GROUP" 0700
    ensure_dir "$LOGDIR" "$SVC_USER" "$SVC_GROUP" 0750
}

# The venv is rebuilt when it is missing, broken, made from another interpreter, or holds a file
# that someone other than root could modify. Nothing in it runs before that last check.
venv_matches() {
    local base bad
    if [ ! -x "$VENV/bin/python" ] || [ ! -f "$VENV/pyvenv.cfg" ]; then return 1; fi
    if bad=$(unsafe_tree "$VENV"); then
        warn "$bad ($(describe "$bad")) can be modified by a user other than root: rebuilding $VENV."
        return 1
    fi
    base=$(awk -F ' = ' '$1 == "executable" { print $2; exit }' "$VENV/pyvenv.cfg") || return 1
    if [ "$base" != "$PY_REAL" ]; then return 1; fi
    if [ -n "$PY_UNSAFE" ] && [ "$AS_ROOT" = 1 ] && [ "$ALLOW_NONROOT_PYTHON" != 1 ]; then return 1; fi
    "$VENV/bin/python" -I -c 'import sys' >/dev/null 2>&1
}

build_venv() {
    section "Code $VENV (root-owned)"
    say "Python: $PY_REAL"
    if [ -n "$PY_UNSAFE" ]; then
        warn "$PY_UNSAFE ($(describe "$PY_UNSAFE")) can be modified by a user other than root,"
        warn "who could then run code as $SVC_USER and read the guard key."
        if [ "$DRY_RUN" = 1 ] && [ "$ALLOW_NONROOT_PYTHON" != 1 ]; then
            warn "A real run refuses this interpreter unless ALLOW_NONROOT_PYTHON=1."
        fi
    fi
    ensure_dir "$PREFIX" root wheel 0755
    if venv_matches; then
        say "reusing $VENV (built from $PY_REAL)"
    else
        run rm -rf "$VENV"
        run "$PY_REAL" -I -m venv "$VENV"
    fi
    run "$VENV/bin/python" -I -m pip --isolated install --no-cache-dir --disable-pip-version-check \
        --progress-bar off --upgrade "$GATEWAY_SRC"
    run chown -R root:wheel "$VENV"
    run chmod -R go-w "$VENV"
    # The service user must be able to run what was built.
    run sudo -u "$SVC_USER" /usr/bin/env -i HOME="$STATE" PATH=/usr/bin:/bin "$VENV/bin/python" -I -c 'import webspec.app'
}

install_plist() {
    section "LaunchDaemon $PLIST_DST"
    PLIST_SAME=0
    if [ -f "$PLIST_DST" ]; then
        if cmp -s "$PLIST_SRC" "$PLIST_DST"; then
            PLIST_SAME=1
        else
            warn "$PLIST_DST differs from $PLIST_SRC and is replaced; edits to it are lost:"
            diff -u "$PLIST_DST" "$PLIST_SRC" >&2 || true
            warn "Put site settings in $ENV_FILE instead: this installer never overwrites it."
        fi
    fi
    run install -m 0644 -o root -g wheel "$PLIST_SRC" "$PLIST_DST"
    run plutil -lint "$PLIST_DST"
}

# ---- Load --------------------------------------------------------------------------------------

# C4 (GD-5): the rule the gateway applies to the WEBSPEC_GUARD_KEY_FILE it reads on every request
# (webspec.config): at most 4096 bytes of UTF-8, a byte order mark ignored; whitespace and format
# characters (Unicode category Cf) around the key removed; then one line of at least 16
# characters with no control or format character in it (64 hex digits recommended). The file
# arrives on standard input and is never printed. Exit 0: a usable key; 10: nothing (blank);
# anything else: not a usable key.
KEY_PROBE='import sys, unicodedata
data = sys.stdin.buffer.read(4097)
if len(data) > 4096:
    sys.exit(11)
try:
    text = data.decode("utf-8-sig")
except UnicodeDecodeError:
    sys.exit(11)
edge = lambda c: c.isspace() or unicodedata.category(c) == "Cf"
i, j = 0, len(text)
while i < j and edge(text[i]):
    i += 1
while j > i and edge(text[j - 1]):
    j -= 1
key = text[i:j]
if not key:
    sys.exit(10)
if len(key.splitlines()) > 1 or len(key) < 16 or any(unicodedata.category(c) in ("Cc", "Cf") for c in key):
    sys.exit(11)'

# What a key file holds ($1, guard.key unless given), by the C4 rule: returns 0 for a usable key,
# 1 for nothing (no file, or a blank one), 2 for something that is not a usable key.
# check_guard_key then has the installed gateway itself load it, as the service user.
key_state() { # key_state [<file>]
    local rc=0 file=${1:-$GUARD_KEY}
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then return 1; fi
    if [ -z "$PY_REAL" ] || { [ -n "$PY_UNSAFE" ] && [ "$AS_ROOT" = 1 ] && [ "$ALLOW_NONROOT_PYTHON" != 1 ]; }; then
        # A dry run as root never executes such an interpreter (select_python): blank or not.
        if grep -q '[^[:space:]]' "$file" 2>/dev/null; then return 0; fi
        return 1
    fi
    "$PY_REAL" -I -c "$KEY_PROBE" <"$file" >/dev/null 2>&1 || rc=$?
    case $rc in
        0) return 0 ;;
        10) return 1 ;;
        *) return 2 ;;
    esac
}

# gateway.env runs as shell code at every start of the daemon. Check it as the daemon runs it: as
# the service user, from the plist's environment, and without printing any of its values. The
# probe prints PATH, then the name of each variable listed after the file that gateway.env
# changes (NAME=value: the plist sets it to value) or sets to something (NAME: the plist does
# not set it), whatever the shell form: export, several on one line, ${NAME:=...}, unset (F46).
# ($1, $w, $n, $s, $c and $PATH are the inner sh's.)
# shellcheck disable=SC2016
ENV_FILE_PROBE='set -ae; . "$1"; set +a; shift
printf "PATH=%s\n" "$PATH"
for w in "$@"; do
    n=${w%%=*}
    eval "s=\${$n+set} c=\${$n-}"
    case $w in
        *=*) if [ "$s" != set ] || [ "$c" != "${w#*=}" ]; then printf "CHANGED %s\n" "$n"; fi ;;
        *) if [ -n "$c" ]; then printf "SET %s\n" "$n"; fi ;;
    esac
done'

# Returns 0 when the daemon may start with gateway.env; 1 when it does not load (the daemon would
# exit at every start: it fails closed); 2 when it puts on PATH a directory, or a command in one,
# that a user other than root can modify (a start would run that user's commands as the service
# user), unless ALLOW_NONROOT_PATH=1.
check_env_file() {
    local out path entry hit bad="" entries name plist_env
    if [ "$DRY_RUN" = 1 ]; then
        say "(a real run first checks that $ENV_FILE loads, and adds to PATH only directories root alone can modify)"
        return 0
    fi
    # The plist's environment, PATH aside (check_plist holds the plist to these values).
    plist_env=("HOME=$STATE" "WEBSPEC_CONFIG=$CONFIG" "WEBSPEC_GUARD_KEY_FILE=$GUARD_KEY"
        "WEBSPEC_AUDIT_LOG=$STATE/gateway-audit.jsonl" "WEBSPEC_APPROVERS_FILE=$SIGNERS"
        "WEBSPEC_SSH_KEYGEN=/usr/bin/ssh-keygen" "WEBSPEC_LAUNCHD_SOCKET=$SOCKET_NAME" "WEBSPEC_PORT=$PORT"
        "WEBSPEC_INTERNAL_PORT=$INTERNAL_PORT")
    if ! out=$(sudo -u "$SVC_USER" /usr/bin/env -i "PATH=$SVC_PATH" "${plist_env[@]}" /bin/sh -c "$ENV_FILE_PROBE" sh \
        "$ENV_FILE" "${plist_env[@]}" WEBSPEC_HOST WEBSPEC_CORS_ORIGINS WEBSPEC_GUARD_KEY \
        WEBSPEC_GUARD_KEY_DEV_EPHEMERAL WEBSPEC_ACCESS_LOG 2>&1); then
        warn "$ENV_FILE does not load in /bin/sh$(error_lines "$out"). Check it with: sudo /bin/sh -n $ENV_FILE"
        return 1
    fi
    case $out in
        PATH=* | *"
PATH="*) ;;
        *)
            # It ended the shell (exit): the daemon's shell would never get to start the gateway.
            warn "$ENV_FILE ends the shell that loads it, so the daemon would never start the gateway."
            return 1
            ;;
    esac
    # What overrides the plist or weakens a default: names only, never values.
    printf '%s\n' "$out" | sed -n -E 's/^(CHANGED|SET) ([A-Za-z_][A-Za-z0-9_]*)$/\2/p' | sort -u |
        while IFS= read -r name; do
            case $name in
                WEBSPEC_LAUNCHD_SOCKET)
                    warn "$ENV_FILE overrides WEBSPEC_LAUNCHD_SOCKET, which the plist sets. The gateway must take"
                    warn "the socket '$SOCKET_NAME' that launchd holds on 127.0.0.1:$INTERNAL_PORT, or another process can"
                    warn "take the port (DP-9)." ;;
                WEBSPEC_HOST)
                    warn "$ENV_FILE sets WEBSPEC_HOST, which has no effect: the gateway listens only on the socket"
                    warn "launchd holds on 127.0.0.1:$INTERNAL_PORT (DP-8)." ;;
                WEBSPEC_CORS_ORIGINS)
                    warn "$ENV_FILE sets WEBSPEC_CORS_ORIGINS: pages from those origins can read the gateway's answers"
                    warn "and send it unsafe methods (DP-8)." ;;
                WEBSPEC_GUARD_KEY)
                    warn "$ENV_FILE sets WEBSPEC_GUARD_KEY, which the gateway uses instead of $GUARD_KEY." ;;
                WEBSPEC_GUARD_KEY_DEV_EPHEMERAL)
                    warn "$ENV_FILE sets WEBSPEC_GUARD_KEY_DEV_EPHEMERAL, an insecure key for local development only." ;;
                WEBSPEC_ACCESS_LOG)
                    warn "$ENV_FILE sets WEBSPEC_ACCESS_LOG; an access log records GET arguments (DP-6)." ;;
                *) warn "$ENV_FILE overrides $name, which the plist sets." ;;
            esac
        done
    path=$(printf '%s\n' "$out" | sed -n 's/^PATH=//p' | tail -n 1)
    case $path in
        "$SVC_PATH" | "$SVC_PATH":*) ;;
        *) warn "$ENV_FILE does not keep the plist's PATH first; append to it instead: PATH=\"\$PATH:/dir\"" ;;
    esac
    case :$path: in
        *::*) bad="an empty entry (the working directory)" ;;
    esac
    IFS=: read -r -a entries <<<"$path"
    for entry in ${entries[@]+"${entries[@]}"}; do
        case $entry in
            '') continue ;;
            /*) entry=$(trim_path "$entry") ;;
            *)
                bad="${bad:+$bad; }$entry (a relative path)"
                continue
                ;;
        esac
        # The directory, and every link it leads through (a link that leads nowhere counts).
        if hit=$(unsafe_links "$entry"); then
            bad="${bad:+$bad; }$entry ($hit is $(what_is "$hit"))"
            continue
        fi
        # A directory gateway.env adds can be root-owned and still hold a command that is not,
        # or a root-owned link into a directory that is not, such as Homebrew's. (The plist's own
        # directories are the system's.)
        case :$SVC_PATH: in
            *:"$entry":*) ;;
            *)
                if hit=$(unsafe_entry "$entry"); then
                    bad="${bad:+$bad; }$entry ($hit)"
                fi
                ;;
        esac
    done
    if [ -z "$bad" ]; then return 0; fi
    warn "PATH from $ENV_FILE has entries that a user other than root can modify: $bad."
    warn "A stdio server found there could be swapped by that user and would run as $SVC_USER, which"
    warn "can read the guard key."
    if [ "$ALLOW_NONROOT_PATH" = 1 ]; then
        warn "Loading anyway because ALLOW_NONROOT_PATH=1."
        return 0
    fi
    return 2
}

# GD-5: the installed gateway, as the service user, must load the key from the file the plist
# names (or from guard.key.new, for --fill-key). A GuardKeyError names the file, never the key;
# any other error is reported by its type alone.
GUARD_KEY_PROBE='import sys
from webspec.config import GuardKeyError, get_session_key
try:
    get_session_key()
except GuardKeyError as exc:
    sys.exit(str(exc))
except Exception as exc:
    sys.exit(type(exc).__name__)'

check_guard_key() { # check_guard_key [<key file>]
    local out file=${1:-$GUARD_KEY}
    if [ "$DRY_RUN" = 1 ]; then
        say "(a real run first checks that the installed gateway, as $SVC_USER, can load the key from $file)"
        return 0
    fi
    if out=$(sudo -u "$SVC_USER" /usr/bin/env -i HOME="$STATE" PATH=/usr/bin:/bin "WEBSPEC_GUARD_KEY_FILE=$file" \
        "$VENV/bin/python" -I -c "$GUARD_KEY_PROBE" 2>&1); then
        return 0
    fi
    warn "the installed gateway, running as $SVC_USER, cannot load the guard key from $file"
    warn "through WEBSPEC_GUARD_KEY_FILE: $out"
    return 1
}

# PIDs listening on TCP port $1, any address.
listeners() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u; }

# The same with each socket's address: "PID address" lines, such as "1 127.0.0.1:7002" or
# "4242 *:7002" (lsof -F prints a p line for each process, then an n line for each socket).
port_listeners() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -F pn 2>/dev/null |
        awk '/^p/ { pid = substr($0, 2) } /^n/ && pid != "" { print pid, substr($0, 2) }' | sort -u
}

# Of the "PID address" lines in $1, those that are not the daemon's socket: 127.0.0.1:$INTERNAL_PORT
# as launchd (PID 1) holds it, and the gateway it hands it to (PID $2). launchd binds the sockets
# of other jobs too, a login user's LaunchAgent's included, so PID 1 at another address is not
# the daemon's.
foreign_in() {
    printf '%s\n' "$1" | awk -v d="$2" -v a="127.0.0.1:$INTERNAL_PORT" 'NF >= 2 && !(($1 == 1 || $1 == d) && $2 == a)'
}

foreign_listeners() { # foreign_listeners <daemon PID, or ->
    local held
    held=$(port_listeners "$INTERNAL_PORT") || held=""
    foreign_in "$held" "$1"
}

describe_pids() {
    local p out=""
    for p in $1; do
        out="$out${out:+, }$p ($(ps -o user= -p "$p" 2>/dev/null | awk '{ print $1 }'), $(ps -o comm= -p "$p" 2>/dev/null))"
    done
    printf '%s\n' "$out"
}

describe_listeners() { # describe_listeners <"PID address" lines>
    local pid addr out=""
    while read -r pid addr; do
        if [ -z "$pid" ]; then continue; fi
        out="$out${out:+, }$(describe_pids "$pid") on $addr"
    done <<EOF
$1
EOF
    printf '%s\n' "$out"
}

# The daemon's PID, or - when it is not running (the first column of `launchctl list`). The list
# is read whole first: awk stops at the label, and under pipefail a launchctl cut short by that
# would make the PID look unknown.
daemon_pid() {
    local out
    out=$(launchctl list) || return 1
    awk -v l="$LABEL" '$3 == l { print $1; found = 1; exit } END { if (!found) print "-" }' <<<"$out"
}

job_loaded() { have launchctl && launchctl print "system/$LABEL" >/dev/null 2>&1; }

# The loaded job holds the gateway's socket, as a job loaded from this plist does: launchctl
# print lists it under sockets as "gateway" = {. In any doubt, no: the job is then reloaded.
job_holds_socket() {
    local out
    out=$(launchctl print "system/$LABEL" 2>/dev/null) || return 1
    case $out in
        *"\"$SOCKET_NAME\" = {"*) return 0 ;;
    esac
    return 1
}

# launchd binds 127.0.0.1:$INTERNAL_PORT for the gateway when the job is loaded, and cannot while
# another process holds that address. A listener on another address of the port, such as
# *:$INTERNAL_PORT, does not hold back the bootstrap: launchd's socket, the more specific one, then
# gets the connections to 127.0.0.1 (if launchd can bind beside it; if not, the bootstrap or the
# health check fails), and check_health names that listener either way. Refusing would leave it
# all of what Caddy forwards (DP-9).
check_gateway_port() {
    local held
    if [ "$DRY_RUN" = 1 ]; then
        say "(a real run first checks that no other process holds 127.0.0.1:$INTERNAL_PORT)"
        return 0
    fi
    held=$(port_listeners "$INTERNAL_PORT") || held=""
    held=$(printf '%s\n' "$held" | awk -v a="127.0.0.1:$INTERNAL_PORT" 'NF >= 2 && $2 == a')
    if [ -z "$held" ]; then return 0; fi
    warn "127.0.0.1:$INTERNAL_PORT is held by $(describe_listeners "$held"). launchd cannot bind it for the"
    warn "gateway, and that process receives what Caddy forwards there. Stop it, then run this installer again."
    return 1
}

# Port $PORT is Caddy's, where Cloudflare Tunnel delivers (DP-5). A gateway listening there, such
# as a development LaunchAgent's (python -m webspec as a login user, the agent's as a rule), is a
# second gateway (DP-7) taking the tunnel's traffic as that user (DP-1): check_dev_agent sees only
# agents that launchd lists, not one started some other way (F48).
check_caddy_port() {
    local held p cmd uid gateways="" logins=""
    local re='(^|[[:space:]])-[A-Za-z]*m[[:space:]]*webspec([[:space:]]|$)'
    if [ "$DRY_RUN" = 1 ]; then
        say "(a real run first checks that no other gateway listens on port $PORT)"
        return 0
    fi
    held=$(listeners "$PORT") || held=""
    if [ -z "$held" ]; then
        warn "nothing listens on port $PORT yet, so any local user can take it: set up Caddy there"
        warn "before you point Cloudflare Tunnel at it."
        return 0
    fi
    for p in $held; do
        cmd=$(ps -o command= -p "$p" 2>/dev/null) || cmd=""
        uid=$(ps -o uid= -p "$p" 2>/dev/null | awk '{ print $1 }') || uid=""
        if [[ $cmd =~ $re ]]; then
            gateways="$gateways${gateways:+ }$p"
            continue
        fi
        case $uid in
            '' | *[!0-9]*) ;;
            *) if [ "$uid" -ge 500 ]; then logins="$logins${logins:+ }$p"; fi ;;
        esac
    done
    if [ -n "$gateways" ]; then
        warn "port $PORT is held by a WebSpec gateway: $(describe_pids "$gateways") runs -m webspec. That is a"
        warn "second gateway (DP-7), most likely a development LaunchAgent, on the port that Caddy and"
        warn "Cloudflare Tunnel use, running as its user (DP-1). Stop it as that user, for example:"
        warn "    /bin/launchctl bootout gui/<uid>/$DEV_LABEL && /bin/launchctl disable gui/<uid>/$DEV_LABEL"
        warn "then run this installer again."
        return 1
    fi
    # Any other login user's process only gets a warning: holding back the daemon would not change
    # who receives the port's traffic, it would stop operators who run Caddy from their admin
    # account, and it would give every login user a way to hold back the daemon.
    if [ -n "$logins" ]; then
        warn "port $PORT is held by $(describe_pids "$logins"), a login user's process. Run Caddy as its own"
        warn "hidden user (DP-5): if that user is the agent's, it receives what Cloudflare Tunnel forwards."
    fi
    say "port $PORT: $(describe_pids "$held")"
}

# Success means the daemon's own process, running as the service user, has taken the socket that
# launchd holds for it on 127.0.0.1:$INTERNAL_PORT, and answers. Nothing else may listen on the
# port, at any address: it would receive what Caddy forwards whenever launchd let go of the port.
# An instance that kickstart -k replaces may first finish the requests in flight, for up to
# EXIT_TIMEOUT seconds, so the wait for its successor is that much longer.
check_health() { # check_health [<PID of the instance just replaced>]
    local i=0 old=${1:--} tries=$HEALTH_TRIES pid="-" held="" other="" owner code mine="127.0.0.1:$INTERNAL_PORT"
    if [ "$DRY_RUN" = 1 ]; then
        say "(a real run then waits until $LABEL itself, as $SVC_USER, answers on $mine)"
        return 0
    fi
    if [ "$old" != - ]; then tries=$((tries + 2 * EXIT_TIMEOUT)); fi
    while [ "$i" -lt "$tries" ]; do
        pid=$(daemon_pid) || pid="-"
        held=$(port_listeners "$INTERNAL_PORT") || held=""
        other=$(foreign_in "$held" "$pid")
        if [ -n "$other" ]; then break; fi
        if [ "$pid" != - ] && [ "$pid" != "$old" ] && has_line "$pid $mine" "$held"; then break; fi
        i=$((i + 1))
        sleep 0.5
    done
    if [ -n "$other" ]; then
        UNHEALTHY=1
        warn "port $INTERNAL_PORT is held by $(describe_listeners "$other"), not only by launchd and $LABEL"
        warn "(PID $pid) on $mine. Stop that process, then: sudo /bin/launchctl kickstart -k system/$LABEL"
        return 0
    fi
    if [ "$pid" = - ] || [ "$pid" = "$old" ] || ! has_line "$pid $mine" "$held"; then
        UNHEALTHY=1
        warn "the gateway has not taken the socket that launchd holds on $mine after"
        warn "$((tries / 2)) s (daemon PID: $pid); see: sudo /usr/bin/tail $LOGDIR/gateway.log"
        return 0
    fi
    owner=$(ps -o user= -p "$pid" | awk '{ print $1 }')
    if [ "$owner" != "$SVC_USER" ]; then
        UNHEALTHY=1
        warn "$LABEL (PID $pid) runs as '$owner', not $SVC_USER"
        return 0
    fi
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H 'Host: localhost' "http://$mine/" 2>/dev/null) || code=000
    if [ "$code" = 000 ]; then
        UNHEALTHY=1
        warn "$LABEL (PID $pid) holds port $INTERNAL_PORT but does not answer HTTP; see: sudo /usr/bin/tail $LOGDIR/gateway.log"
        return 0
    fi
    say "the gateway (PID $pid, user $owner) answers on $mine (GET / for Host localhost: HTTP $code)"
}

# launchctl bootout may return before the job is gone ("Operation now in progress"): launchd
# kills the gateway EXIT_TIMEOUT seconds (the plist's ExitTimeOut) after asking it to stop, so
# this waits longer than that. Returns 1 if the job is still loaded then.
wait_unloaded() {
    local i=0
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    while launchctl print "system/$LABEL" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt "$UNLOAD_TRIES" ]; then return 1; fi
        sleep 0.25
    done
}

# load_daemon does not (re)start the daemon (F13, DP-9). One that is not loaded is disabled, so
# that launchd does not load it at the next boot either; the enable before the next bootstrap
# undoes that. A loaded one is left as it is: launchd keeps holding its socket, which a bootout
# would free for any local process to take, and to receive what Caddy forwards there for as long
# as it holds it. Leaving it is safe when the reason is another process (a development gateway,
# which any login user, the agent's included, can start, and which a bootout would not stop), and
# when the reason is the key or a gateway.env that does not load ("config"): with either, the
# gateway fails closed. A PATH that another user can modify is not safe to start with: stop_daemon.
hold_back() { # hold_back <reason> [config]
    if [ "$WAS_LOADED" != 1 ]; then
        say "not loading: $1"
        run launchctl disable "system/$LABEL"
        say "$LABEL stays disabled, at boot too, until a run of this installer passes these checks."
        return 0
    fi
    HELD=1
    say "not restarting $LABEL: $1"
    if [ "$DRY_RUN" = 1 ] || job_holds_socket; then
        say "It stays loaded as it is, and launchd keeps holding 127.0.0.1:$INTERNAL_PORT for it: a bootout would"
        say "free the port for any local user to take."
    else
        # Loaded from a plist without the socket: the gateway holds the port itself.
        say "It stays loaded as it is: a bootout would free 127.0.0.1:$INTERNAL_PORT for any local user to take."
    fi
    if [ "${2:-}" = config ]; then
        say "With what this run refused, the gateway fails closed: it refuses every request that needs the"
        say "key (it reads $GUARD_KEY on each one) or, should it restart, does not start at all."
    else
        say "What this run installed takes effect when the gateway next starts."
    fi
    fail "$LABEL was not restarted: it runs as it did before this run (see above)"
}

# gateway.env puts on PATH what a user other than root can modify (check_env_file returned 2): any
# start of the daemon would run that user's commands as the service user, which can read the
# guard key, so a loaded daemon is not left for its next KeepAlive restart. It is disabled first,
# so that a bootout that fails or takes long still keeps launchd from loading it at the next
# boot, then booted out. Not while another process listens on its port, at any address: that
# process would receive what Caddy forwards from the moment launchd let go of the port.
stop_daemon() { # stop_daemon <reason>
    local pid others
    if [ "$WAS_LOADED" != 1 ]; then
        hold_back "$1"
        return 0
    fi
    say "stopping $LABEL: $1"
    run launchctl disable "system/$LABEL"
    if [ "$DRY_RUN" != 1 ]; then
        pid=$(daemon_pid) || pid=-
        others=$(foreign_listeners "$pid")
        if [ -n "$others" ]; then
            HELD=1
            warn "not booting out $LABEL: port $INTERNAL_PORT is also held by $(describe_listeners "$others"),"
            warn "which would receive what Caddy forwards to 127.0.0.1:$INTERNAL_PORT once launchd let go of it."
            warn "Stop that process, then run this installer again. Until then $LABEL keeps running, disabled"
            warn "at boot; should it restart (a crash, kickstart -k), its stdio servers run from that PATH."
            fail "$LABEL was not stopped: another process listens on port $INTERNAL_PORT (see above)"
            return 0
        fi
    fi
    run launchctl bootout "system/$LABEL" || true
    if ! wait_unloaded; then
        HELD=1
        warn "$LABEL is still loaded $((UNLOAD_TRIES / 4)) s after the bootout. It is disabled, so launchd does"
        warn "not load it at the next boot. See: sudo /bin/launchctl print system/$LABEL"
        fail "$LABEL could not be booted out (see above)"
        return 0
    fi
    STOPPED=1
    warn "with $LABEL booted out, nothing holds 127.0.0.1:$INTERNAL_PORT: any local user can take it and"
    warn "receive what Caddy forwards there. Stop Caddy, or the tunnel, until the gateway runs again."
    if [ "$DRY_RUN" != 1 ]; then
        others=$(port_listeners "$INTERNAL_PORT") || others=""
        if [ -n "$others" ]; then warn "Port $INTERNAL_PORT is already held by $(describe_listeners "$others")."; fi
    fi
    say "$LABEL stays disabled, at boot too, until a run of this installer passes these checks."
    fail "$LABEL was running, and is stopped and disabled now (see above)"
}

load_daemon() {
    local env_rc=0 old others
    section "Load $LABEL"
    WAS_LOADED=0
    if job_loaded; then WAS_LOADED=1; fi
    if [ "$DRY_RUN" = 1 ] && [ -d "$ETC" ] && { [ ! -x "$ETC" ] || { [ -e "$GUARD_KEY" ] && [ ! -r "$GUARD_KEY" ]; }; }; then
        # Without root a dry run cannot read guard.key, and everything below depends on it.
        KEY_STATE=3
        say "($GUARD_KEY cannot be read without root, and what a real run does from here depends on it"
        say "and on checks that need root as well: run the dry run with sudo to see that part of the plan.)"
        return 0
    fi
    # 1. The daemon's own configuration, which only root can change (DP-4).
    KEY_STATE=0
    key_state || KEY_STATE=$?
    # gateway.env before the key: a PATH there that another user can modify stops a loaded
    # daemon whatever else this run refuses, or its next start (a crash, a reboot) runs from it.
    check_env_file || env_rc=$?
    if [ "$env_rc" = 2 ]; then
        stop_daemon "use directories that only root can modify, or set ALLOW_NONROOT_PATH=1"
        return 0
    fi
    case $KEY_STATE in
        0) ;;
        1)
            hold_back "$GUARD_KEY is empty" config
            return 0
            ;;
        *)
            hold_back "$GUARD_KEY does not hold a usable key: one line of 16 characters or more, without control characters (64 hex digits is best)" config
            return 0
            ;;
    esac
    if [ "$env_rc" != 0 ]; then
        hold_back "$ENV_FILE does not load, so the daemon would exit at every start" config
        return 0
    fi
    if ! check_guard_key; then
        hold_back "the gateway would refuse every request that needs the key (GD-5)" config
        return 0
    fi
    # 2. Other processes, which any login user can start, the agent's included (F48).
    if [ -n "$DEV_AGENT" ]; then
        hold_back "the development LaunchAgent $DEV_LABEL is loaded for a login user (see the warning above)"
        return 0
    fi
    if ! check_caddy_port; then
        hold_back "another gateway listens on port $PORT"
        return 0
    fi
    # 3. (Re)start it.
    if [ "$WAS_LOADED" = 1 ]; then
        if [ "$PLIST_SAME" = 1 ] && job_holds_socket; then
            # The loaded job is this plist's: restart only the gateway, which takes the socket
            # again. launchd holds 127.0.0.1:$INTERNAL_PORT all along, so no other process can
            # take the port meanwhile (DP-9), as it could between a bootout and a bootstrap.
            old=$(daemon_pid) || old=-
            run launchctl enable "system/$LABEL"
            if ! run launchctl kickstart -k "system/$LABEL"; then
                warn "launchctl kickstart failed; see: sudo /bin/launchctl print system/$LABEL"
                fail "$LABEL could not be restarted (see above)"
                return 0
            fi
            LOADED=1
            check_health "$old"
            return 0
        fi
        # Loaded from another plist, or without the socket: reload it. Nothing holds the port from
        # the bootout to the bootstrap, so not while another process listens on it, at any
        # address: that process would receive what Caddy forwards in the gap.
        if [ "$DRY_RUN" = 1 ]; then
            say "(a real run first checks that no other process listens on port $INTERNAL_PORT)"
        else
            old=$(daemon_pid) || old=-
            others=$(foreign_listeners "$old")
            if [ -n "$others" ]; then
                warn "port $INTERNAL_PORT is also held by $(describe_listeners "$others"), which would receive"
                warn "what Caddy forwards between the bootout of $LABEL and its bootstrap. Stop it, then run this"
                warn "installer again."
                hold_back "another process listens on port $INTERNAL_PORT"
                return 0
            fi
        fi
        run launchctl bootout "system/$LABEL" || true
        if ! wait_unloaded; then
            warn "$LABEL is still loaded $((UNLOAD_TRIES / 4)) s after the bootout: not reloading it. See:"
            warn "    sudo /bin/launchctl print system/$LABEL"
            fail "$LABEL could not be reloaded (see above)"
            return 0
        fi
    fi
    run launchctl enable "system/$LABEL"
    if ! check_gateway_port; then
        # Its own checks passed, so it stays enabled: at the next boot launchd loads it before any
        # login, and holds the port first.
        say "not loading: another process holds 127.0.0.1:$INTERNAL_PORT"
        say "$LABEL is enabled: launchd loads it at the next boot, before any login."
        if [ "$WAS_LOADED" = 1 ]; then STOPPED=1; fi
        fail "another process holds 127.0.0.1:$INTERNAL_PORT, where Caddy forwards (see above)"
        return 0
    fi
    if ! run launchctl bootstrap system "$PLIST_DST"; then
        if [ "$WAS_LOADED" = 1 ]; then STOPPED=1; fi
        warn "launchctl bootstrap failed; see: sudo /bin/launchctl print system/$LABEL"
        others=$(port_listeners "$INTERNAL_PORT") || others=""
        if [ -n "$others" ]; then warn "Port $INTERNAL_PORT is held by $(describe_listeners "$others")."; fi
        fail "$LABEL could not be loaded (see above)"
        return 0
    fi
    LOADED=1
    check_health
}

# ---- Guard key (--fill-key) ---------------------------------------------------------------------
#
# F50, C4: the operator fills or rotates the key by piping it in from the password manager:
#   /usr/local/bin/op read 'op://<vault>/<item>/<field>' | sudo <this script> --fill-key
# The gateway reads guard.key on every request, so what lands there takes effect at once. The
# whole input (up to the 4096 bytes that C4 allows, and one more to tell) goes to guard.key.new,
# where the C4 rule (key_state) and the installed gateway itself, as the service user
# (check_guard_key), judge it. Only a key that both accept replaces guard.key, in one rename.
# Anything else leaves the old key in place: a failed read (op signed out, Touch ID refused), a
# blank or malformed line, or a secret of several lines, which is refused whole, never cut to
# its first line (the first line of an SSH or PEM key is public).
fill_key_main() {
    local p
    if [ "$DRY_RUN" = 1 ]; then die "--fill-key has no dry run: it reads the key and replaces $GUARD_KEY"; fi
    if [ "$AS_ROOT" != 1 ]; then
        die "must be run as root: <password manager> | sudo $SCRIPT_DIR/install.sh --fill-key"
    fi
    if [ "$(uname -s)" != Darwin ]; then die "this installer is for macOS; see gateway/deploy/ for other platforms"; fi
    cd /
    for p in "$ETC" "$GUARD_KEY" "$GUARD_KEY.new" "$VENV"; do
        if [ -L "$p" ]; then die "$p is a symbolic link; this installer manages only real files and directories"; fi
    done
    check_source
    select_python
    fill_key
}

fill_key() {
    local new=$GUARD_KEY.new state=0
    if [ -t 0 ]; then
        die "--fill-key reads the key from standard input: pipe it in from your password manager, e.g.
    /usr/local/bin/op read 'op://<vault>/<item>/<field>' | sudo $SCRIPT_DIR/install.sh --fill-key"
    fi
    if [ ! -d "$ETC" ] || [ ! -x "$VENV/bin/python" ] || ! record_exists "/Users/$SVC_USER"; then
        die "nothing is installed here yet: run sudo $SCRIPT_DIR/install.sh first, then fill the key"
    fi
    FILL_NEW=$new
    trap 'if [ -n "$FILL_NEW" ]; then rm -f "$FILL_NEW"; fi' EXIT
    rm -f "$new"
    printf '+ head -c 4097 > %s  (from standard input)\n' "$(shquote "$new")"
    (umask 077 && head -c 4097 >"$new")
    run chown "root:$SVC_GROUP" "$new"
    run chmod 0440 "$new"
    key_state "$new" || state=$?
    case $state in
        0) ;;
        1) refuse_key "there is no key on standard input" ;;
        *) refuse_key "standard input does not hold a usable key: one line of 16 characters or more, without control characters (64 hex digits is best)" ;;
    esac
    check_guard_key "$new" || refuse_key "the installed gateway does not accept it"
    run mv -f "$new" "$GUARD_KEY"
    FILL_NEW=""
    say "$GUARD_KEY holds the new key."
    if env_file_sets_key; then
        warn "$ENV_FILE sets WEBSPEC_GUARD_KEY, which the gateway uses instead of $GUARD_KEY: the new key"
        warn "takes effect only once you remove that line and run sudo $SCRIPT_DIR/install.sh again."
    elif job_loaded; then
        say "The gateway reads it on every request: the new key is in effect now."
    else
        say "Load the daemon now: sudo $SCRIPT_DIR/install.sh"
    fi
}

# env_file_sets_key succeeds when gateway.env, loaded as the daemon loads it, sets
# WEBSPEC_GUARD_KEY, which the gateway takes in place of guard.key (GD-5).
env_file_sets_key() {
    local out
    [ -f "$ENV_FILE" ] || return 1
    out=$(sudo -u "$SVC_USER" /usr/bin/env -i "PATH=$SVC_PATH" /bin/sh -c "$ENV_FILE_PROBE" sh "$ENV_FILE" \
        WEBSPEC_GUARD_KEY 2>/dev/null) || return 1
    printf '%s\n' "$out" | grep -qx 'SET WEBSPEC_GUARD_KEY'
}

refuse_key() {
    if [ -n "$FILL_NEW" ]; then rm -f "$FILL_NEW"; fi
    FILL_NEW=""
    die "$1; $GUARD_KEY is unchanged"
}

next_steps() {
    local agent
    section "Next steps"
    say "Give sudo every command by its absolute path, as below: sudo looks a bare name up on"
    say "your PATH, and the agent may be able to write a directory on it."
    if [ -n "$DEV_AGENT" ]; then
        say "- Boot out the development LaunchAgent (as its user, not root), then run this again:"
        while IFS= read -r agent; do
            if [ -n "$agent" ]; then say "      /bin/launchctl bootout $agent && /bin/launchctl disable $agent"; fi
        done <<EOF
$DEV_AGENT
EOF
    fi
    case $KEY_STATE in
        0)
            say "- Rotate the guard key straight from your password manager (never through argv or history):"
            say "  the gateway reads $GUARD_KEY on every request, so the new key applies at once."
            ;;
        3)
            say "- Fill or rotate the guard key straight from your password manager (never through argv or"
            say "  history); without root this dry run cannot tell whether $GUARD_KEY holds one."
            ;;
        *)
            say "- Fill the guard key straight from your password manager (never through argv or history),"
            say "  then run this installer again to (re)start the daemon."
            ;;
    esac
    cat <<EOF
  With the 1Password CLI from its .pkg, which installs a root-owned /usr/local/bin/op:
      /usr/local/bin/op read 'op://<vault>/<item>/<field>' | sudo $SCRIPT_DIR/install.sh --fill-key
  This replaces $GUARD_KEY in one step, and only with a key that the installed gateway
  accepts: one line of 16 characters or more, without control characters (64 hex digits is
  best). Anything else, a failed read included, leaves the old key in place. An op that your
  own user can modify, such as Homebrew's, can be swapped by the agent; then do this step
  from another account.
EOF
    if [ "$LOADED" != 1 ] && [ "$KEY_STATE" = 0 ] && [ -z "$DEV_AGENT" ]; then
        say "- Fix what the warnings above name, then run this installer again."
        if [ "$HELD" = 1 ]; then say "  Until then $LABEL keeps running as it did before this run."; fi
    fi
    if [ "$STOPPED" = 1 ]; then
        say "- This run stopped $LABEL. Until it runs again, Caddy forwards to whatever holds"
        say "  127.0.0.1:$INTERNAL_PORT: stop Caddy, or the tunnel, until then."
    fi
    cat <<EOF
- Edit the files in $ETC as root with a fixed editor, for example:
      sudo -H /usr/bin/vi $CONFIG
  Never with sudo -e (sudoedit): it gives a copy of the file to your own user, the one the
  agent may share, and runs your editor with your environment.
- MCP servers go in config.json (format: config.example.json). Their secrets go in
  gateway.env as NAME='value', referenced as "\${NAME}" in a stdio server's env or an http
  server's headers, never in args: any local user can read a process's arguments.
- A stdio server runs as $SVC_USER and can read the guard key. Give it an absolute command,
  and install its program and code as root, outside every user's home ($PREFIX/services,
  for example). If it needs node, npx or op on PATH, link them from a directory that only
  root can modify and append that directory in gateway.env: PATH="\$PATH:$PREFIX/bin".
- After changing gateway.env, run this installer again: it checks the file, then restarts the
  gateway while launchd keeps holding its port (config.json is re-read every 30 s anyway).
  This restarts it without those checks:
      sudo /bin/launchctl kickstart -k system/$LABEL
- Level 4: list approvers in $SIGNERS. Until it names one, requests that
  need an approval are refused with 503 approval_unavailable (AP-7).
- These macOS files alone do not meet DP-5 and DP-6: this installer ships no proxy, and
  webspec-ctl manages Caddy (setup-caddy.sh, and the site blocks and reloads of webspec-ctl
  add) only on Linux with systemd, so do not use those parts of it here. Provide the proxy
  yourself, for example Caddy as its own hidden user, from a binary only root can modify,
  listening on loopback only, on 127.0.0.1:$PORT and [::1]:$PORT. It must forward only
  hosts under your domain to 127.0.0.1:$INTERNAL_PORT, with the Host header kept, and
  *.localhost names only for connections from this host, answering 421 to one that
  carries Cloudflare's Cf-Ray header (DP-5). It must keep query strings, request and
  response headers, and userinfo out of its logs (DP-6). Point Cloudflare Tunnel at it only then:
  until something listens on port $PORT, any local user can take that port.
- DP-3 is not set up here: block the agent's other network egress (firewall or egress
  proxy) so the gateway is its only way out.
- $LOGDIR/gateway.log is not rotated: launchd keeps it open, so newsyslog would
  only move the live file. To trim it, work as $SVC_USER (never as root in a directory that
  $SVC_USER controls):
      sudo -u $SVC_USER /bin/cp $LOGDIR/gateway.log $LOGDIR/gateway.log.1
      sudo -u $SVC_USER /bin/sh -c ': >$LOGDIR/gateway.log'
- Check: sudo /bin/launchctl print system/$LABEL
         sudo /usr/bin/tail -f $LOGDIR/gateway.log
EOF
}

# A real run exits 1 when the daemon it leaves is not one that runs this run's installation: not
# healthy, stopped, left running as it was before (F13), or kept from its address. A run that
# only leaves a daemon that was not loaded disabled, as a first install does until guard.key is
# filled, is no failure.
finish() {
    if [ "$UNHEALTHY" = 1 ]; then fail "installed, but the gateway is not healthy (see the warnings above)"; fi
    if [ -z "$FAILURE" ]; then return 0; fi
    if [ "$DRY_RUN" = 1 ]; then
        printf '\n(a real run would exit 1 here: %s)\n' "$FAILURE"
        return 0
    fi
    printf '\ninstall.sh: %s\n' "$FAILURE" >&2
    exit 1
}

main() {
    parse_args "$@"
    if [ "$FILL_KEY" = 1 ]; then
        fill_key_main
        return 0
    fi
    preflight
    check_dev_agent
    ensure_account
    install_files
    build_venv
    install_plist
    load_daemon
    next_steps
    finish
}

# Sourcing defines the functions without running anything (the tests use this).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
