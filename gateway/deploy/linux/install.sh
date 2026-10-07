#!/bin/bash
# Install the WebSpec gateway as a hardened systemd system service (Linux).
#
#   sudo gateway/deploy/linux/install.sh [OPTION]...   install, or upgrade in place
#   gateway/deploy/linux/install.sh --dry-run          print the plan; changes nothing, needs no root
#
# Options:
#   -n, --dry-run            print the plan instead of changing anything (DRY_RUN=1 does the same)
#   --python=PATH            the Python 3.11+ to build the venv with (default: python3 on the
#                            PATH below; a Python in /usr/local or /opt must be named by its path)
#   --allow-nonroot-python   accept a Python whose files a user other than root can change
#   --allow-existing-user    adopt an existing webspec account that looks like a login account
#   --allow-drop-ins         keep drop-ins for the gateway's units in /etc and /run, which are
#                            refused otherwise; the units must still run what this script installs
#   -h, --help               print this text
#
# Run it with plain sudo, never sudo -E. As root it starts over in an empty environment with
# PATH=/usr/sbin:/usr/bin:/sbin:/bin and HOME=/root, keeping only DRY_RUN and the proxy
# variables (http_proxy, https_proxy, no_proxy and their upper-case forms), so nothing the
# invoking user left in the environment (that user may be the agent's) steers what root runs
# or what pip installs: no PATH, no PIP_INDEX_URL, no pip.conf in that user's home. That is
# also why the overrides above are options and not variables: under sudo -E or a sudoers
# env_keep, a variable could come from the agent and name the interpreter root runs. pip runs
# with --isolated and reads only the global configuration (/etc/pip.conf,
# /etc/xdg/pip/pip.conf), which must be root's alone.
#
# What it sets up (docs/spec/audit-deployment.md DP-1, DP-2, DP-4, DP-6, DP-7, DP-8 and DP-9,
# and GD-5 in docs/spec/levels.md):
#   user webspec:webspec       system user the gateway runs as, never the agent's user (DP-1)
#   /opt/webspec/venv          the gateway's code, root-owned and read-only to the service
#   /etc/webspec/              root:webspec 0750, read-only to the service (DP-4)
#     config.json              root:webspec 0640  MCP servers, created with none; secrets only as "${NAME}"
#     config.example.json      root:root    0644  the entry format, refreshed on every run
#     gateway.env              root:root    0600  WEBSPEC_GUARD_KEY (GD-5), WEBSPEC_DOMAIN, secrets
#     allowed_signers          root:root    0644  public keys of the level-4 approvers
#   /var/lib/webspec/          webspec 0700       HOME and gateway-audit.jsonl
#   /etc/systemd/system/webspec-gateway.socket    systemd holds 127.0.0.1:7002 for the gateway (DP-9);
#                                                 enabled and started on every run
#   /etc/systemd/system/webspec-gateway.service   enabled and (re)started once gateway.env holds
#                                                 WEBSPEC_GUARD_KEY; it never starts without a
#                                                 key it can use
# Not set up here: Caddy in front of the gateway (DP-5, and DP-6 for its logs), which
# gateway/tools/setup-caddy.sh installs, and the agent's network egress (DP-3), which is the
# operator's to confine.
#
# It is idempotent: a re-run upgrades the code and the units and re-applies owners and modes,
# but never overwrites config.json, gateway.env or allowed_signers. systemd holds the gateway's
# port from the first run on, key or no key, so that no other process can take what Caddy
# forwards there (DP-9). The port changes hands only when it must (the first time, from a
# gateway that bound it itself, or when the socket's directives change), and
# cloudflared.service is stopped for that moment. The gateway is enabled and (re)started only
# once gateway.env holds a WEBSPEC_GUARD_KEY (GD-5), one that is more than whitespace and
# invisible characters, and then checked: it runs as webspec and answers on the socket that
# systemd holds. A gateway that exits instead, as it does for a key it cannot use, fails the run.
#
# Before it changes anything, it refuses (DP-1, DP-4): an interpreter, standard library or
# libpython that a user other than root can change, there or where a symbolic link in them
# leads (Debian's sitecustomize.py is a link into /etc), and likewise the directory of
# ensurepip's wheels that the interpreter names (--allow-nonroot-python overrides); a global pip
# configuration that a user other than root can change, or where its link leads; an existing
# webspec account that someone can log in to (--allow-existing-user overrides); and a running or
# enabled webspec-gateway.service that it did not install or that runs as another user, such
# as the development gateway run as a system unit (its unit file is saved as
# webspec-gateway.service.bak.<time>). Site directories of the interpreter that others can
# change only get a warning, since root runs the interpreter with -I -S and the venv has its
# own: on a Debian host that keeps /etc/staff-group-for-usr-local, the group staff can change
# /usr/local and the site directory in it. It never runs anything in an existing venv that a
# user other than root can change: it moves that venv aside and builds a new one, as it does
# for a venv of another interpreter or Python X.Y, or one without a working pip. A new venv
# takes the old one's place only once it has its packages and imports; until then the old one
# is kept aside, and put back if the build fails. It enables nothing while a drop-in in /etc or
# /run changes the units (--allow-drop-ins keeps them), or while the units would run something
# else, as someone else, with another environment file, or listen elsewhere.
#
# Root runs this script and builds, installs and starts the gateway from the checkout it is
# in, so whoever can change the checkout controls the gateway (DP-1, DP-4). The script
# refuses a checkout that a user other than root could change: a file or directory in it
# that root does not own or that others can write, a symlink in gateway/, or a directory
# above it that others can write (/tmp too). That catches running it from the agent's
# working tree. It cannot tell whether someone changed the files before root got them:
# install from a fresh clone of a release you have reviewed, made by root.
set -euo pipefail

# F14: the overrides are options. A variable of the earlier interface is ignored, and said so:
# under sudo -E it may come from another user's environment. (Builtins only, before the
# environment is cleaned.)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  for _name in $(compgen -e); do
    case "$_name" in
      PYTHON | ALLOW_NONROOT_PYTHON | ALLOW_EXISTING_USER)
        printf 'install.sh: ignoring %s from the environment: the overrides are options (see --help)\n' "$_name" >&2
        ;;
    esac
  done
fi
# DP-1, DP-4: as root, start over in a clean environment before anything else runs. sudo -E,
# or a sudoers env_keep, would otherwise pass the invoking user's PATH, HOME (and with it
# ~/.config/pip/pip.conf), PIP_* variables and anything else to the commands below.
if ((EUID == 0)) && [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  for _name in $(compgen -e); do
    case "$_name" in
      PATH | HOME | PWD | OLDPWD | SHLVL | _ | DRY_RUN | WEBSPEC_INSTALL_CLEAN | http_proxy | https_proxy | \
        no_proxy | HTTP_PROXY | HTTPS_PROXY | NO_PROXY) ;;
      *)
        if [[ -n "${WEBSPEC_INSTALL_CLEAN:-}" ]]; then
          printf 'install.sh: %s is still set in the clean environment\n' "$_name" >&2
          exit 1
        fi
        exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root WEBSPEC_INSTALL_CLEAN=1 \
          ${DRY_RUN+"DRY_RUN=$DRY_RUN"} \
          ${http_proxy+"http_proxy=$http_proxy"} ${https_proxy+"https_proxy=$https_proxy"} \
          ${no_proxy+"no_proxy=$no_proxy"} ${HTTP_PROXY+"HTTP_PROXY=$HTTP_PROXY"} \
          ${HTTPS_PROXY+"HTTPS_PROXY=$HTTPS_PROXY"} ${NO_PROXY+"NO_PROXY=$NO_PROXY"} \
          /bin/bash "$0" "$@"
        ;;
    esac
  done
  PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root
  export PATH HOME
  unset WEBSPEC_INSTALL_CLEAN
fi
# With --isolated, pip reads no PIP_* variable and no file of the user's; what root installs
# comes from where the global configuration points pip, which must be root's (see
# pip_config_unsafe), or from PyPI.
for _name in $(compgen -e); do
  case "$_name" in PIP_*) unset "$_name" ;; esac
done
umask 022 # the venv must be readable by the service user, whatever root's umask is

readonly SVC_USER=webspec SVC_GROUP=webspec
readonly PREFIX=/opt/webspec VENV=/opt/webspec/venv
readonly CONF_DIR=/etc/webspec STATE_DIR=/var/lib/webspec
readonly CONFIG=/etc/webspec/config.json
readonly CONFIG_REF=/etc/webspec/config.example.json
readonly ENV_FILE=/etc/webspec/gateway.env
readonly SIGNERS=/etc/webspec/allowed_signers
readonly UNIT=webspec-gateway.service SOCKET=webspec-gateway.socket
readonly UNIT_PATH=/etc/systemd/system/webspec-gateway.service
readonly SOCKET_PATH=/etc/systemd/system/webspec-gateway.socket
readonly GATEWAY_PORT=7002 # webspec-gateway.socket: ListenStream=127.0.0.1:7002
# The tunnel in front of Caddy. It is stopped while 127.0.0.1:7002 changes hands, as
# setup-caddy.sh stops it while Caddy's ports do.
readonly TUNNEL=cloudflared.service
# The first line of every production unit this script installs. A loaded webspec-gateway.service
# whose file lacks it is someone else's: the development gateway, run as a system unit.
readonly UNIT_MARKER='# WebSpec gateway: production systemd SYSTEM unit (Linux).'
# The guard-key check that webspec-gateway.service runs before the gateway
# (ExecStartPre=/bin/sh -c '<this>'), as systemd shows it (GD-5). Keep the two in step.
# shellcheck disable=SC2016 # expanded by the unit's shell, never here
readonly KEY_CHECK='case "$${WEBSPEC_GUARD_KEY-}" in *[![:space:]]*) exit 0 ;; esac; echo "WEBSPEC_GUARD_KEY is empty or missing in /etc/webspec/gateway.env: not starting (GD-5)" >&2; exit 1'
# What the gateway counts as no key besides ASCII whitespace (webspec/config.py, _invisible:
# Unicode whitespace and format characters, category Cf), as bash patterns over the bytes of
# their UTF-8 encodings (LC_ALL=C), for guard_key_set: a copy from a web page or an editor can
# leave U+200B, U+00A0 or U+FEFF where the key belongs. Every code point that Python 3.11 to
# 3.14 counts, so U+13439-1343F as well, which 3.11 leaves unassigned (BLANK_RULE in
# tests/test_deploy_linux.py, which checks each one and its neighbours).
readonly BLANK_UTF8=(
  $'[\x1c\x1d\x1e\x1f]'                                      # U+001C-001F
  $'\xc2[\x85\xa0\xad]'                                      # U+0085, U+00A0, U+00AD
  $'\xd8[\x80\x81\x82\x83\x84\x85\x9c]'                      # U+0600-0605, U+061C
  $'\xdb\x9d' $'\xdc\x8f'                                    # U+06DD, U+070F
  $'\xe0\xa2[\x90\x91]' $'\xe0\xa3\xa2'                      # U+0890-0891, U+08E2
  $'\xe1\x9a\x80' $'\xe1\xa0\x8e'                            # U+1680, U+180E
  $'\xe2\x80[\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x8b\x8c\x8d\x8e\x8f]' # U+2000-200F
  $'\xe2\x80[\xa8\xa9\xaa\xab\xac\xad\xae\xaf]'              # U+2028-202F
  $'\xe2\x81[\x9f\xa0\xa1\xa2\xa3\xa4\xa6\xa7\xa8\xa9\xaa\xab\xac\xad\xae\xaf]' # U+205F-2064, U+2066-206F
  $'\xe3\x80\x80' $'\xef\xbb\xbf' $'\xef\xbf[\xb9\xba\xbb]'  # U+3000, U+FEFF, U+FFF9-FFFB
  $'\xf0\x91\x82\xbd' $'\xf0\x91\x83\x8d'                    # U+110BD, U+110CD
  $'\xf0\x93\x90[\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xbb\xbc\xbd\xbe\xbf]' # U+13430-1343F
  $'\xf0\x9b\xb2[\xa0\xa1\xa2\xa3]'                          # U+1BCA0-1BCA3
  $'\xf0\x9d\x85[\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba]'          # U+1D173-1D17A
  $'\xf3\xa0\x80[\x81\xa0\xa1\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xab\xac\xad\xae\xaf]' # U+E0001, U+E0020-E002F
  $'\xf3\xa0\x80[\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xbb\xbc\xbd\xbe\xbf]' # U+E0030-E003F
  $'\xf3\xa0\x81[\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x8b\x8c\x8d\x8e\x8f]' # U+E0040-E004F
  $'\xf3\xa0\x81[\x90\x91\x92\x93\x94\x95\x96\x97\x98\x99\x9a\x9b\x9c\x9d\x9e\x9f]' # U+E0050-E005F
  $'\xf3\xa0\x81[\xa0\xa1\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xab\xac\xad\xae\xaf]' # U+E0060-E006F
  $'\xf3\xa0\x81[\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xbb\xbc\xbd\xbe\xbf]' # U+E0070-E007F
)
# The deployment guide, which the next steps point to.
readonly GUIDE=https://i-m-a-g-i-n-e.github.io/WebSpec/guide/deploy/
LOGIN_DEFS=/etc/login.defs
# The options (see --help).
OPT_PYTHON=python3 OPT_ALLOW_NONROOT_PYTHON=0 OPT_ALLOW_EXISTING_USER=0 OPT_ALLOW_DROP_INS=0
STAMP="$(date +%Y%m%d-%H%M%S)"
readonly STAMP

HERE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
GATEWAY_SRC="$(CDPATH='' cd -- "$HERE/../.." && pwd -P)" # the repository's gateway/ directory
CHECKOUT="$(CDPATH='' cd -- "$GATEWAY_SRC/.." && pwd -P)" # the repository
readonly HERE GATEWAY_SRC CHECKOUT
# Python puts the working directory first on sys.path, and root's may be one the agent can
# write (/tmp, a shared home): a venv.py or pip/ there would run as root. So work from /, and
# run every Python isolated (-I: no working directory, PYTHON* variables or user site), and
# the interpreter that builds the venv without site processing as well (-S): its site
# directories are not the venv's, and a .pth file in one runs code (Debian puts
# /usr/local/lib/python3.X/dist-packages there).
cd /

# Anything but an empty value or 0 means a dry run, so a typo never runs the real thing.
case "${DRY_RUN:-0}" in
  0 | "") DRY=0 ;;
  *) DRY=1 ;;
esac

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err() { printf 'install.sh: %s\n' "$*" >&2; }
die() {
  err "$@"
  exit 1
}

# run CMD... runs CMD, or prints it in a dry run.
run() {
  if ((DRY)); then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# quiet CMD... runs CMD and shows its output only if it fails. A dry run prints CMD.
quiet() {
  local out
  if ((DRY)); then
    run "$@"
  elif ! out="$("$@" 2>&1)"; then
    printf '%s\n' "$out" >&2
    die "failed: $*"
  fi
}

# when DESCRIPTION CMD... succeeds when CMD does. A dry run plans for a fresh host: it
# prints the condition and succeeds, so the plan shows every step.
when() {
  local what="$1"
  shift
  if ((DRY)); then
    printf '# if %s:\n' "$what"
    return 0
  fi
  "$@"
}

# need DESCRIPTION CMD... stops unless CMD succeeds; want only warns. A dry run lists them.
need() {
  local what="$1"
  shift
  if ((DRY)); then
    printf '# check: %s\n' "$what"
  elif ! "$@" >/dev/null 2>&1; then
    die "check failed: $what"
  fi
}
want() {
  local what="$1"
  shift
  if ((DRY)); then
    printf '# check (warning only): %s\n' "$what"
  elif ! "$@" >/dev/null 2>&1; then
    warn "check failed: $what"
  fi
}

# write_file MODE OWNER GROUP DEST installs stdin as DEST. A dry run prints the content.
write_file() {
  if ((DRY)); then
    printf '+ install -m %s -o %s -g %s /dev/stdin %s <<EOF\n' "$1" "$2" "$3" "$4"
    cat
    printf 'EOF\n'
  else
    install -m "$1" -o "$2" -g "$3" /dev/stdin "$4"
  fi
}

missing() { [[ ! -e "$1" && ! -L "$1" ]]; }
no_group() { ! getent group "$1" >/dev/null; }
no_user() { ! id -u "$1" >/dev/null 2>&1; }
no_symlinks() {
  local f
  for f in "$@"; do
    [[ ! -L "$f" ]] || return 1
  done
}
have() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || return 1
  done
}
describe() {
  stat -c '%U:%G %A' -- "$1" 2>/dev/null || stat -f '%Su:%Sg %Sp' -- "$1" 2>/dev/null || printf 'unknown owner'
}

# parse_args ARG... sets the options (see --help).
parse_args() {
  while (($#)); do
    case "$1" in
      -n | --dry-run) DRY=1 ;;
      --python=*) OPT_PYTHON="${1#--python=}" ;;
      --python)
        (($# > 1)) || die "--python needs a value (see --help)"
        OPT_PYTHON="$2"
        shift
        ;;
      --allow-nonroot-python) OPT_ALLOW_NONROOT_PYTHON=1 ;;
      --allow-existing-user) OPT_ALLOW_EXISTING_USER=1 ;;
      --allow-drop-ins) OPT_ALLOW_DROP_INS=1 ;;
      -h | --help)
        sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
        exit 0
        ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  [[ -n "$OPT_PYTHON" ]] || die "--python needs a value (see --help)"
}

# Prints each path through which a user other than root could change what root runs, builds
# and installs from the checkout: in it, everything (symlinks aside) that root does not own
# or that its group or others can write, and every symlink in gateway/ (it could point
# anywhere); above it, every directory that root does not own or that others can write. A
# directory above the checkout counts even when it is sticky, like /tmp: someone who can
# swap or add entries there can swap the whole tree, or plant the .gitignore or .hgignore
# that the build looks for in every directory above gateway/. Fails if find cannot look.
# (-perm -020 -o -perm -002: group- or world-writable, in a form every find accepts.)
changeable_by_others() {
  local dir="$CHECKOUT"
  find "$CHECKOUT" ! -type l \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print || return 1
  find "$GATEWAY_SRC" -type l -print || return 1
  while [[ "$dir" != / ]]; do
    dir="$(dirname -- "$dir")"
    find "$dir" -maxdepth 0 \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print || return 1
  done
}

# refuse_checkout PATHS explains why the checkout is refused, showing the first few PATHS.
refuse_checkout() {
  local path shown=0 total
  total="$(grep -c '' <<<"$1")"
  err "refusing to install from $CHECKOUT: users other than root can change it."
  cat >&2 <<EOF
Root runs this script and builds, installs and starts the gateway from this checkout, so
whoever can change it controls the gateway (DP-1, DP-4). Not owned by root, or writable
by others:
EOF
  while IFS= read -r path && ((shown < 10)); do
    printf '  %s\n' "$(ls -ld -- "$path" 2>&1)" >&2
    shown=$((shown + 1))
  done <<<"$1"
  if ((total > shown)); then
    printf '  (and %d more)\n' "$((total - shown))" >&2
  fi
  cat >&2 <<'EOF'
Install from a checkout that only root can change, such as a fresh clone of a release you
have reviewed:
  sudo git clone https://github.com/I-m-A-g-I-n-E/WebSpec.git /opt/webspec/src
  sudo /opt/webspec/src/gateway/deploy/linux/install.sh
EOF
}

# unsafe_node PATH succeeds when a user other than root could change PATH itself: one that root
# does not own, or that its group or others can write (/tmp too). A symbolic link counts by its
# owner (its mode means nothing); where it leads is unsafe_path's. One find cannot look at counts.
unsafe_node() {
  local hit
  if [[ -L "$1" ]]; then
    hit="$(find "$1" -maxdepth 0 ! -uid 0 -print 2>/dev/null)" || hit="$1"
  else
    hit="$(find "$1" -maxdepth 0 \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print 2>/dev/null)" || hit="$1"
  fi
  [[ -n "$hit" ]]
}

# unsafe_chain PATH prints the first of PATH and the directories above it, as written, that a
# user other than root could change (unsafe_node). Fails when none.
unsafe_chain() {
  local p="$1"
  while ! unsafe_node "$p"; do
    [[ "$p" != / ]] || return 1
    p="$(dirname -- "$p")"
  done
  printf '%s\n' "$p"
}

# unsafe_path PATH prints the first path through which a user other than root could change what
# the absolute PATH names. It resolves PATH one name at a time, as the kernel does, and judges
# with unsafe_node every directory on the way, every symbolic link on the way, and the file at
# the end. Each link is followed where it leads, so a root-owned link counts when what it leads
# to, or any directory on the way there, is someone else's to change (P6: Debian's
# /usr/lib/python3.X/sitecustomize.py leads to /etc/python3.X). A name that does not exist is
# judged by the directory it would be created in, judged already; links that lead round in a
# loop (more than 40) count. Fails when there is none.
unsafe_path() {
  local rest="$1" at="" name next link hops=0
  if [[ "$rest" != /* ]]; then
    printf '%s\n' "$1" # relative: whoever picks the working directory picks the file
    return 0
  fi
  if unsafe_node /; then
    printf '/\n'
    return 0
  fi
  # $at is where the names so far lead, a physical path ("" for /).
  while [[ -n "$rest" ]]; do
    name="${rest%%/*}"
    rest="${rest#"$name"}"
    rest="${rest#/}"
    case "$name" in
      "" | .) continue ;;
      ..)
        at="${at%/*}"
        continue
        ;;
    esac
    next="$at/$name"
    if [[ -L "$next" ]]; then
      if unsafe_node "$next"; then
        printf '%s\n' "$next"
        return 0
      fi
      hops=$((hops + 1))
      if ((hops > 40)) || ! link="$(readlink -- "$next")"; then
        printf '%s\n' "$next"
        return 0
      fi
      [[ "$link" != /* ]] || at=""
      rest="$link/$rest"
    elif [[ -e "$next" ]]; then
      if unsafe_node "$next"; then
        printf '%s\n' "$next"
        return 0
      fi
      at="$next"
    else
      return 1
    fi
  done
  return 1
}

# unsafe_entries DIR prints the first entry of DIR (DIR included) that a user other than root
# could change: one root does not own (a symbolic link too), or, a link aside, one that its
# group or others can write. Fails when there is none; a tree it cannot search counts.
unsafe_entries() {
  local hit
  hit="$(find -H "$1" \( ! -uid 0 -o ! -type l \( -perm -020 -o -perm -002 \) \) -print -quit 2>/dev/null)" ||
    hit="$1"
  [[ -n "$hit" ]] || return 1
  printf '%s\n' "$hit"
}

# under PATH DIR... succeeds when PATH is one of the DIRs (empty ones aside) or lies below one.
under() {
  local p="$1/" d
  shift
  for d in "$@"; do
    [[ -n "$d" ]] || continue
    case "$p" in "${d%/}/"*) return 0 ;; esac
  done
  return 1
}

# unsafe_tree DIR prints the first path through which a user other than root could change what
# DIR holds: an entry that unsafe_entries finds, or, for each symbolic link in it, what
# unsafe_path finds on the way to where the link leads (P6). A directory that a link leads to
# is searched in turn, once (links that lead back up end there), unless a search already
# covers it. Fails when there is none; a tree it cannot search counts.
unsafe_tree() {
  local i=0 dir link to
  local -a todo seen
  todo=("$1")
  to="$(readlink -f -- "$1" 2>/dev/null)" || to="$1"
  seen=("$to")
  while ((i < ${#todo[@]})); do
    dir="${todo[i]}"
    i=$((i + 1))
    unsafe_entries "$dir" && return 0
    while IFS= read -r -d '' link; do
      unsafe_path "$link" && return 0
      to="$(readlink -f -- "$link" 2>/dev/null)" || continue
      if [[ -d "$to" ]] && ! under "$to" "${seen[@]}"; then
        todo+=("$to")
        seen+=("$to")
      fi
    done < <(find -H "$dir" -type l -print0 2>/dev/null)
  done
  return 1
}

# --- Python and pip (DP-1, DP-4) ------------------------------------------------------
# A venv does not copy the interpreter or its standard library: the gateway runs them from
# where they are, as webspec, next to the guard key. Whoever can change them runs code as
# the gateway, so a real run refuses an interpreter that a user other than root could
# change, unless --allow-nonroot-python, and executes nothing of it before it has checked.

# The interpreter that builds the venv, as a physical path: the venv records it and runs it
# (pyvenv.cfg, bin/python), so no symbolic link that someone could repoint stays in between.
resolve_python() {
  local p
  case "$OPT_PYTHON" in
    /*) p="$OPT_PYTHON" ;;
    */*) return 1 ;; # relative: whoever picks the working directory picks the interpreter
    *) p="$(command -v -- "$OPT_PYTHON")" || return 1 ;;
  esac
  p="$(readlink -f -- "$p")" || return 1
  [[ "$p" == /* && -f "$p" && -x "$p" ]] || return 1
  printf '%s\n' "$p"
}

# python_libs REAL prints the standard library and libpython of the interpreter REAL (a
# physical path, <prefix>/bin/pythonX.Y) that exist: <prefix>/lib/python3*,
# <prefix>/lib64/python3*, and libpython3* in <prefix>/lib, <prefix>/lib64 and <prefix>/lib/*/.
python_libs() {
  local prefix d
  prefix="$(dirname -- "$(dirname -- "$1")")"
  for d in "$prefix"/lib/python3* "$prefix"/lib64/python3* \
    "$prefix"/lib/libpython3* "$prefix"/lib64/libpython3* "$prefix"/lib/*/libpython3*; do
    if [[ -e "$d" || -L "$d" ]]; then
      printf '%s\n' "$d"
    fi
  done
}

# python_unsafe REAL prints the first path through which a user other than root could change
# what the interpreter REAL (a physical path) runs: the executable and every directory above
# it; its standard library and libpython (python_libs), the directories on the way to them,
# and wherever their symbolic links lead (unsafe_path, unsafe_tree); and, once all of that is
# cleared and not before, the directory of ensurepip's wheels that REAL names when it runs
# isolated and without site processing (-I -S), Debian's /usr/share/python-wheels: ensurepip
# builds the venv's pip from them, which root then runs (P6). Fails when none. Its site
# directories are site_unsafe's.
python_unsafe() {
  local d real
  local -a searched=()
  unsafe_chain "$1" && return 0
  while IFS= read -r d; do
    unsafe_path "$d" && return 0
    unsafe_tree "$d" && return 0
    if real="$(readlink -f -- "$d")"; then
      searched+=("$real")
    fi
  done < <(python_libs "$1")
  d="$("$1" -I -S -c 'import sysconfig; print(sysconfig.get_config_var("WHEEL_PKG_DIR") or "")' 2>/dev/null)" ||
    d=""
  if [[ -n "$d" ]] && [[ -e "$d" || -L "$d" ]] &&
    ! under "$(readlink -f -- "$d")" ${searched[@]+"${searched[@]}"}; then
    unsafe_path "$d" && return 0
    unsafe_tree "$d" && return 0
  fi
  return 1
}

# site_unsafe REAL prints a site directory of the interpreter REAL that a user other than root
# could change, and on a second line the first path through which they could (unsafe_path,
# unsafe_tree). Those are the directories REAL names when it runs with -I -S, outside its
# standard library (python_unsafe judges what lies in that): Debian's
# /usr/local/lib/python3.X/dist-packages, which the group staff can change through /usr/local on
# a host that keeps /etc/staff-group-for-usr-local. Neither this script nor the gateway reads
# them, so they are a warning, not a refusal (P6): root runs REAL only with -I -S, and the venv
# has a site directory of its own. Run REAL only once python_unsafe has cleared it. Fails when
# none.
site_unsafe() {
  local d bad real
  local -a libs=()
  while IFS= read -r d; do
    if real="$(readlink -f -- "$d")"; then
      libs+=("$real")
    fi
  done < <(python_libs "$1")
  while IFS= read -r d; do
    if [[ -z "$d" ]] || [[ ! -e "$d" && ! -L "$d" ]]; then
      continue
    fi
    if under "$(readlink -f -- "$d")" ${libs[@]+"${libs[@]}"}; then
      continue # /usr/lib/python3/dist-packages, say: python_unsafe searched it
    fi
    if bad="$(unsafe_path "$d")" || bad="$(unsafe_tree "$d")"; then
      printf '%s\n%s\n' "$d" "$bad"
      return 0
    fi
  done < <("$1" -I -S -c 'import site; print(*site.getsitepackages(), sep="\n")' 2>/dev/null)
  return 1
}

# refuse_python BAD explains why the interpreter is refused.
refuse_python() {
  err "refusing $OPT_PYTHON ($PY_REAL): $1 is $(describe "$1"), so a user other than root can change it."
  cat >&2 <<EOF
The gateway runs this interpreter and its standard library as $SVC_USER, next to the guard
key, and root runs it to build the venv, which gets its pip from ensurepip's wheels. So
whoever can change them, or where their symbolic links lead, controls the gateway or root
(DP-1, DP-4). Use a Python only root can change, such as the distribution's
(apt install python3 python3-venv), and name it if it is not python3 on $PATH:
  sudo $0 --python=/usr/bin/python3.12
Or add --allow-nonroot-python to accept the risk when the agent never runs as that user.
EOF
}

# check_python sets PY_REAL (the interpreter, a physical path) and PY_VERSION (X.Y.Z), or
# stops. Nothing of the interpreter runs before python_unsafe has cleared it, and root runs
# it only with -I -S. Site directories that others can change get a warning (site_unsafe).
check_python() {
  local bad site
  if PY_REAL="$(resolve_python)"; then
    if bad="$(python_unsafe "$PY_REAL")"; then
      if ((OPT_ALLOW_NONROOT_PYTHON)); then
        warn "$bad ($(describe "$bad")) can be changed by a user other than root, who could then run code as $SVC_USER (DP-1). Going ahead because of --allow-nonroot-python."
      elif ((DRY)); then
        warn "$bad ($(describe "$bad")) can be changed by a user other than root: a real run refuses $PY_REAL unless --allow-nonroot-python."
      else
        refuse_python "$bad"
        exit 1
      fi
    elif site="$(site_unsafe "$PY_REAL")"; then
      bad="${site#*$'\n'}" site="${site%%$'\n'*}"
      warn "users other than root can change $site, a site directory of $PY_REAL ($bad is $(describe "$bad")). This installer and the gateway never read it: root runs $PY_REAL only with -I -S, and the venv has a site directory of its own. Anything else run with $PY_REAL reads it, such as a stdio MCP server, which runs as $SVC_USER (DP-1): give such a server a venv of its own."
    fi
  elif ((DRY)); then
    PY_REAL="$OPT_PYTHON"
  else
    die "--python=$OPT_PYTHON is not a Python found on $PATH or named by an absolute path (DP-1: name the one to build with, e.g. --python=/usr/bin/python3.12)"
  fi
  if ((DRY)); then
    say "# check: only root can change $PY_REAL, the directories above it, its standard library and libpython, and where their symbolic links lead, and then, run with -I -S to name it, the directory of ensurepip's wheels (or --allow-nonroot-python); nothing of it runs before"
    say "# check (warning only): only root can change the site directories that $PY_REAL, run with -I -S, names; neither this script nor the gateway reads them"
  fi
  need "$PY_REAL is Python 3.11 or newer and has venv (Debian/Ubuntu: apt install python3-venv)" \
    "$PY_REAL" -I -S -c 'import ensurepip, sys, venv; sys.exit(sys.version_info < (3, 11))'
  PY_VERSION=""
  if ((!DRY)); then
    PY_VERSION="$("$PY_REAL" -I -S -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  fi
}

# pip_config_unsafe prints the first global pip configuration file that root's pip would read
# (/etc/pip.conf, /etc/xdg/pip/pip.conf), or the directory where one could be put, that a user
# other than root can change: it could send root's pip to another index (DP-1, DP-4). One that
# is a symbolic link counts by where it leads as well (unsafe_path). Fails when there is none.
# (--isolated skips the user's files and PIP_* variables, not these.)
GLOBAL_PIP_CONFIGS="/etc/pip.conf /etc/xdg/pip/pip.conf"
pip_config_unsafe() {
  local f p
  for f in $GLOBAL_PIP_CONFIGS; do
    p="$f"
    while [[ ! -e "$p" && ! -L "$p" ]]; do
      p="$(dirname -- "$p")"
    done
    unsafe_chain "$p" && return 0
    if [[ -L "$p" ]]; then
      unsafe_path "$p" && return 0
    fi
  done
  return 1
}

check_pip_config() {
  local bad
  if ((DRY)); then
    say "# check: only root can change the global pip configuration (/etc/pip.conf, /etc/xdg/pip/pip.conf), which pip reads as root"
  elif bad="$(pip_config_unsafe)"; then
    die "$bad is $(describe "$bad"): a user other than root could point root's pip at another index, and so run code as root and as $SVC_USER (DP-1, DP-4). Fix that first."
  fi
}

# --- The service account (DP-1) ---------------------------------------------------------

# account_problems prints, one per line, why the existing account $SVC_USER does not look
# like a system account that nobody can log in to. Nothing when it does, or does not exist.
account_problems() {
  local entry uid home shell pw uid_min others
  entry="$(getent passwd "$SVC_USER")" || return 0
  IFS=: read -r _ _ uid _ _ home shell <<<"$entry"
  uid_min="$(awk '$1 == "UID_MIN" { print $2; exit }' "$LOGIN_DEFS" 2>/dev/null)" || uid_min=""
  [[ "$uid_min" =~ ^[0-9]+$ ]] || uid_min=1000
  if ((uid == 0)); then
    say "it has UID 0 (root)"
  elif ((uid >= uid_min)); then
    say "its UID $uid is in the range of login accounts ($uid_min and up)"
  fi
  others="$(getent passwd | awk -F: -v u="$uid" -v n="$SVC_USER" '$3 == u && $1 != n { print $1 }' | paste -sd, -)"
  [[ -z "$others" ]] || say "it shares UID $uid with $others"
  case "$shell" in
    */nologin | */false) ;;
    *) say "its login shell is ${shell:-/bin/sh (none set)}" ;;
  esac
  [[ "$home" == "$STATE_DIR" ]] || say "its home is ${home:-(none)}, not $STATE_DIR"
  if ! pw="$(getent shadow "$SVC_USER")"; then
    say "its password entry cannot be read"
  else
    pw="$(cut -d: -f2 <<<"$pw")"
    case "$pw" in
      '!'* | '*'*) ;;
      '') say "it has an empty password, so it can log in without one" ;;
      *) say "it has a password" ;;
    esac
  fi
}

# check_account stops unless an existing $SVC_USER looks like a system account nobody can log
# in to, or --allow-existing-user adopts it anyway.
check_account() {
  local problems line
  if ((DRY)); then
    say "# check: an existing user $SVC_USER is a system account with no password and no login shell, its home $STATE_DIR (or --allow-existing-user)"
    return 0
  fi
  problems="$(account_problems)"
  [[ -n "$problems" ]] || return 0
  if ((OPT_ALLOW_EXISTING_USER)); then
    while IFS= read -r line; do
      warn "the existing user $SVC_USER: $line. Adopted because of --allow-existing-user."
    done <<<"$problems"
    return 0
  fi
  err "refusing to adopt the existing user $SVC_USER as the gateway's service account (DP-1):"
  while IFS= read -r line; do
    printf '  %s\n' "$line" >&2
  done <<<"$problems"
  cat >&2 <<EOF
The gateway runs as $SVC_USER. Whoever can log in as that user shares the gateway's user,
which DP-1 rules out: processes of one user can read each other's environment, and the
guard key is in the gateway's. Remove or rename that account and run this installer again
(it creates a system account with no password and no login shell), or add
--allow-existing-user if only root can act as it.
EOF
  exit 1
}

# --- A gateway this script did not install (DP-1, DP-4) ---------------------------------

# production_unit FILE succeeds when FILE is a regular file that starts like the units this
# script installs.
production_unit() {
  [[ -f "$1" && ! -L "$1" ]] && [[ "$(head -n 1 -- "$1")" == "$UNIT_MARKER" ]]
}

pid_uid() { awk '$1 == "Uid:" { print $3; exit }' "/proc/$1/status" 2>/dev/null; }
user_name() { getent passwd "$1" | cut -d: -f1 | grep . || printf 'UID %s\n' "${1:-unknown}"; }

# foreign_unit succeeds when there is a webspec-gateway.service that is not this script's
# gateway running as webspec: a loaded unit whose file is not a production unit at $UNIT_PATH,
# or, whatever its load state (a deleted or masked unit file leaves its process running), a
# main process that runs as another user. That is the development gateway run as a system
# unit (the earlier setup-caddy.sh required one), which runs as a login user and reads that
# user's ~/.claude.json. Sets FOREIGN_WHY, FOREIGN_FILE (the unit file to save, if it is
# loaded) and FOREIGN_LIVE (1 when it is running or enabled).
foreign_unit() {
  local line key value load="" frag="" active="" enabled="" pid=0 uid why=""
  FOREIGN_WHY="" FOREIGN_FILE="" FOREIGN_LIVE=0
  while IFS= read -r line; do
    key="${line%%=*}" value="${line#*=}"
    case "$key" in
      LoadState) load="$value" ;;
      FragmentPath) frag="$value" ;;
      ActiveState) active="$value" ;;
      UnitFileState) enabled="$value" ;;
      MainPID) pid="$value" ;;
    esac
  done < <(systemctl show "$UNIT" -p LoadState -p FragmentPath -p ActiveState -p UnitFileState -p MainPID 2>/dev/null)
  if [[ "$load" == loaded ]] && { [[ "$frag" != "$UNIT_PATH" ]] || ! production_unit "$frag"; }; then
    why="its unit file ${frag:-(none)} is not one this installer wrote"
  fi
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    uid="$(pid_uid "$pid")" || uid=""
    if [[ -z "$uid" || "$uid" != "$(id -u "$SVC_USER" 2>/dev/null)" ]]; then
      why="${why:+$why, and }its main process (PID $pid) runs as $(user_name "$uid"), not $SVC_USER"
      if [[ "$load" != loaded ]]; then
        why="$why; its unit is ${load:-unknown} (its file was removed or masked while it ran)"
      fi
    fi
  fi
  [[ -n "$why" ]] || return 1
  FOREIGN_WHY="$why"
  [[ "$load" != loaded ]] || FOREIGN_FILE="$frag"
  case "$active:$enabled" in
    active:* | activating:* | deactivating:* | reloading:* | refreshing:* | *:enabled* | *:linked*) FOREIGN_LIVE=1 ;;
  esac
}

# backup_unit saves FOREIGN_FILE, readable by root only (an old unit may hold a key), and
# prints where.
backup_unit() {
  local backup="$UNIT_PATH.bak.$STAMP"
  [[ -n "$FOREIGN_FILE" && -f "$FOREIGN_FILE" ]] || return 0
  install -m 0600 -o root -g root "$FOREIGN_FILE" "$backup"
  printf '%s\n' "$backup"
}

# refuse_foreign_unit WHY BACKUP explains the refusal, and how to switch over so that
# 127.0.0.1:$GATEWAY_PORT, where an existing Caddy forwards, is free only for a moment (DP-9).
refuse_foreign_unit() {
  local why="$1" backup="$2" stop="sudo systemctl disable --now $UNIT"
  [[ -n "$FOREIGN_FILE" ]] || stop="sudo systemctl stop $UNIT"
  err "refusing to install: $UNIT is not this installer's gateway running as $SVC_USER: $why."
  cat >&2 <<EOF
It is probably the development gateway run as a system unit. That one runs as a login user
(the agent's, perhaps) and reads that user's ~/.claude.json and ~/.webspec/gateway.env, so
it meets neither DP-1 nor DP-4; under this unit's name it would keep running unnoticed.
Nothing was changed${backup:+; its unit file is saved as $backup}.
Switch over so that 127.0.0.1:$GATEWAY_PORT, where Caddy forwards, is free only for a moment
(DP-9); any local process could take it then:
  1. While it still runs, write the new configuration as root (this installer never
     overwrites it): move its MCP servers from that user's ~/.claude.json into $CONFIG
     (the format: $GATEWAY_SRC/deploy/config.example.json), each secret written as
     "\${NAME}", and put NAME=value and the guard key in $ENV_FILE:
       sudo install -d -m 0700 $CONF_DIR
       sudo -H /usr/bin/vi $CONFIG
       sudo -H /usr/bin/vi $ENV_FILE
       op read 'op://WebSpec/gateway-guard/key' |
         sudo sh -c 'IFS= read -r k || [ -n "\$k" ] && printf "WEBSPEC_GUARD_KEY=%s\\n" "\$k" >> $ENV_FILE'
  2. Stop the tunnel, so that nothing reaches the port while it is free:
       sudo systemctl stop $TUNNEL
  3. Stop the old gateway and run this installer again at once; systemd then holds the port:
       $stop && sudo $0
  4. Start the tunnel again: sudo systemctl start $TUNNEL
EOF
}

check_foreign_unit() {
  FOREIGN_WHY="" FOREIGN_FILE="" FOREIGN_LIVE=0
  if ((DRY)); then
    say "# check: no $UNIT that this installer did not write, or that runs as another user than $SVC_USER, is running or enabled (a development gateway run as a system unit)"
    return 0
  fi
  foreign_unit || return 0
  if ((FOREIGN_LIVE)); then
    refuse_foreign_unit "$FOREIGN_WHY" "$(backup_unit)"
    exit 1
  fi
  say "Replacing the stopped and disabled $UNIT: $FOREIGN_WHY"
}

# Prints user@UID.service for each user manager that runs a webspec-gateway.service: the
# single-user development unit (gateway/systemd/), which listens on the same port. Best
# effort, from the cgroup of every process, so it needs neither D-Bus nor the user's session.
running_user_units() {
  { grep -h -o -E 'user@[0-9]+\.service/([^/]+/)*webspec-gateway\.service(/|$)' /proc/[0-9]*/cgroup 2>/dev/null |
    grep -o -E '^user@[0-9]+\.service' | sort -u; } || true
}

# guard_key_set FILE succeeds when the last WEBSPEC_GUARD_KEY= line of FILE has a value the
# gateway can use, as far as bytes tell: something besides quotes, whitespace and the other
# characters the gateway counts as no key (BLANK_UTF8), such as a lone U+200B (P8). The
# gateway's own rule has the last word: under the unit it exits rather than serve without a
# usable key (WEBSPEC_REQUIRE_GUARD_KEY=1), and verify_gateway reports that. Never prints the
# value. (A subshell: the C locale, in which the patterns match bytes, ends with it.)
guard_key_set() (
  export LC_ALL=C
  line="$(grep -E '^[[:space:]]*WEBSPEC_GUARD_KEY=' "$1" | tail -n 1)" || exit 1
  value="${line#*=}"
  value="${value//[[:space:]\"\']/}"
  for blank in "${BLANK_UTF8[@]}"; do
    # shellcheck disable=SC2295 # a pattern, not a string
    value="${value//$blank/}"
  done
  [[ -n "$value" ]]
)

# --- The venv (DP-1, DP-4) --------------------------------------------------------------

# move_aside SRC DEST renames SRC (a symbolic link stays one) to DEST, a new name under the
# root-owned $PREFIX: whatever DEST held is removed first, so SRC never lands inside it.
move_aside() {
  [[ ! -e "$2" && ! -L "$2" ]] || rm -rf -- "$2"
  mv -- "$1" "$2"
}

# venv_stale DIR prints why the root-owned venv DIR cannot be reused for $PY_REAL, and fails
# when it can: built from another interpreter or another Python X.Y (a distribution upgrade
# removes the old one; a patch release of the same X.Y keeps the venv working), unable to
# run, or without a working pip (a build that did not finish). Its python runs only after
# everything else matched.
venv_stale() {
  local cfg="$1/pyvenv.cfg" exe ver
  if [[ ! -f "$cfg" || -L "$cfg" ]]; then
    say "it has no pyvenv.cfg"
    return 0
  fi
  exe="$(awk -F ' = ' '$1 == "executable" { print $2; exit }' "$cfg")"
  ver="$(awk -F ' = ' '$1 == "version" { print $2; exit }' "$cfg")"
  if [[ "$exe" != "$PY_REAL" ]]; then
    say "it was built from ${exe:-an unknown Python}, not $PY_REAL"
  elif [[ "$(cut -d. -f1-2 <<<"$ver")" != "$(cut -d. -f1-2 <<<"$PY_VERSION")" ]]; then
    say "it was built for Python ${ver:-(unknown)}, not $PY_VERSION"
  elif [[ "$(readlink -f -- "$1/bin/python" 2>/dev/null)" != "$PY_REAL" ]]; then
    say "its bin/python is not $PY_REAL"
  elif ! "$1/bin/python" -I -c 'import sys' >/dev/null 2>&1; then
    say "its python does not run"
  elif ! "$1/bin/python" -I -m pip --version >/dev/null 2>&1; then
    say "it has no working pip (a build that did not finish)"
  else
    return 1
  fi
}

# prepare_venv DIR leaves at DIR a venv that only root can change and that runs $PY_REAL: the
# one there, or a new one. Nothing in the old one runs before both are known. A new one is
# built in the old one's place (a venv cannot be moved: its scripts name its path), and the
# old one waits aside (VENV_PREVIOUS) until the new one has its packages and imports:
# venv_done then drops it, and venv_rollback, run on any exit before that, puts it back. So
# a failed or interrupted build never leaves a venv without pip or without the gateway (F18).
VENV_BUILDING=0 VENV_TARGET="" VENV_PREVIOUS=""
prepare_venv() {
  local dir="$1" bad="" why
  VENV_TARGET="$dir" VENV_PREVIOUS=""
  # Its own entries (unsafe_entries). Its links lead to the interpreter, which is check_python's
  # to judge (and --allow-nonroot-python's to accept), and venv_stale's to match with $PY_REAL.
  if [[ -L "$dir" ]] || { [[ -e "$dir" ]] && bad="$(unsafe_entries "$dir")"; }; then
    # Someone other than root could have put code in it: run none of it, and never put it back.
    warn "${bad:-$dir} ($(describe "${bad:-$dir}")) can be changed by a user other than root: none of $dir is run; it is moved to $dir.untrusted.$STAMP and built anew (DP-1, DP-4)."
    move_aside "$dir" "$dir.untrusted.$STAMP"
    rm -rf -- "$dir.untrusted.$STAMP" || warn "could not remove $dir.untrusted.$STAMP; remove it by hand"
  fi
  if [[ -e "$dir" ]]; then
    if ! why="$(venv_stale "$dir")"; then
      say "Reusing $dir (Python $PY_VERSION, $PY_REAL)"
      return 0
    fi
    VENV_PREVIOUS="$dir.previous.$STAMP"
    say "Rebuilding $dir: $why. The old one waits as $VENV_PREVIOUS until the new one works."
    move_aside "$dir" "$VENV_PREVIOUS"
  fi
  VENV_BUILDING=1
  "$PY_REAL" -I -S -m venv "$dir"
}

# venv_rollback removes an unfinished new venv and puts the previous one back (the EXIT trap
# while a build runs).
venv_rollback() {
  ((VENV_BUILDING)) || return 0
  VENV_BUILDING=0
  rm -rf -- "$VENV_TARGET" || true
  if [[ -n "$VENV_PREVIOUS" && -d "$VENV_PREVIOUS" ]]; then
    if mv -- "$VENV_PREVIOUS" "$VENV_TARGET"; then
      warn "the new venv was not finished; the previous one is back at $VENV_TARGET."
    fi
  else
    warn "the new venv was not finished and is removed; run this installer again."
  fi
}

# venv_done: the new venv has its packages and imports, so the previous one goes.
venv_done() {
  ((VENV_BUILDING)) || return 0
  VENV_BUILDING=0
  if [[ -n "$VENV_PREVIOUS" ]]; then
    rm -rf -- "$VENV_PREVIOUS" || warn "could not remove $VENV_PREVIOUS; remove it by hand"
  fi
}

# remove_leftovers removes what an interrupted earlier run left aside. Only root can create
# entries in $PREFIX by now, and none of them is ever run.
remove_leftovers() {
  local d
  for d in "$VENV".previous.* "$VENV".untrusted.*; do
    [[ -e "$d" || -L "$d" ]] || continue
    say "Removing $d, left by an interrupted run"
    rm -rf -- "$d"
  done
}

# --- The units (DP-1, DP-4, DP-9) -------------------------------------------------------

# prop UNIT PROPERTY prints the property as systemd shows it, one value per line.
prop() { systemctl show -p "$2" --value "$1" 2>/dev/null || true; }

# exec_config UNIT PROPERTY prints each command of an Exec*Ex property, one per line, without
# its run-time fields: "{ path=... ; argv[]=... ; flags=...".
exec_config() { prop "$1" "$2" | sed -e 's/ ; start_time=.*$//'; }

# drop_ins UNIT prints the unit's drop-ins in /etc and /run (any but the distribution's, in
# /usr/lib/systemd and /lib/systemd): what an earlier setup, an edit (systemctl edit,
# systemctl set-property) or a generator left there.
drop_ins() {
  local p
  for p in $(prop "$1" DropInPaths); do
    case "$p" in
      /usr/lib/systemd/* | /lib/systemd/*) ;;
      *) printf '%s\n' "$p" ;;
    esac
  done
}

# unit_lines KEY: the values of the KEY= lines of the service unit this script installs, joined by
# spaces, as systemctl show prints them (the shipped values hold no spaces or quotes).
unit_lines() {
  sed -n "s/^$1=//p" "$HERE/$UNIT" | paste -sd ' ' -
}

# effective_unit_problems prints, one per line, where the installed units as systemd runs them
# (drop-ins included) differ from what this script installs in what DP-1, DP-4, DP-9 and GD-5
# rest on: who runs what, from which environment files and with which guard-key settings, in
# which sandbox, listening where.
# Drop-ins outlive the unit files: an EnvironmentFile= in a login user's home, say, is read
# after gateway.env and can choose the guard key. Nothing when they match.
effective_unit_problems() {
  local unit expect value p
  if ((!OPT_ALLOW_DROP_INS)); then
    for unit in "$UNIT" "$SOCKET"; do
      for p in $(drop_ins "$unit"); do
        say "$unit has the drop-in $p"
      done
    done
  fi
  for unit in "$UNIT:$UNIT_PATH" "$SOCKET:$SOCKET_PATH"; do
    value="$(prop "${unit%%:*}" LoadState)"
    [[ "$value" == loaded ]] || say "${unit%%:*} is ${value:-not known to systemd}, not loaded"
    value="$(prop "${unit%%:*}" FragmentPath)"
    [[ "$value" == "${unit#*:}" ]] || say "systemd loads ${unit%%:*} from ${value:-nowhere}, not ${unit#*:}"
  done
  value="$(prop "$UNIT" User):$(prop "$UNIT" Group)"
  [[ "$value" == "$SVC_USER:$SVC_GROUP" ]] || say "$UNIT runs as $value, not $SVC_USER:$SVC_GROUP"
  value="$(prop "$UNIT" DynamicUser)"
  [[ "$value" == no ]] || say "$UNIT has DynamicUser=$value"
  expect="{ path=$VENV/bin/python ; argv[]=$VENV/bin/python -I -m webspec ; flags="
  value="$(exec_config "$UNIT" ExecStartEx)"
  [[ "$value" == "$expect" ]] || say "$UNIT runs ExecStart=${value:-(nothing)}, not $expect"
  expect="{ path=/bin/sh ; argv[]=/bin/sh -c $KEY_CHECK ; flags="
  value="$(exec_config "$UNIT" ExecStartPreEx)"
  [[ "$value" == "$expect" ]] || say "$UNIT runs ExecStartPre=${value:-(nothing)}, not only its guard-key check"
  for p in ExecConditionEx ExecStartPostEx ExecReloadEx ExecStopEx ExecStopPostEx; do
    value="$(exec_config "$UNIT" "$p")"
    [[ -z "$value" ]] || say "$UNIT runs ${p%Ex}=$value"
  done
  value="$(prop "$UNIT" EnvironmentFiles)"
  [[ "$value" == "$ENV_FILE (ignore_errors=no)" ]] || say "$UNIT reads the environment files ${value//$'\n'/, }, not $ENV_FILE alone"
  # GD-5 (P8): the gateway exits rather than serve without a usable WEBSPEC_GUARD_KEY, and no
  # other source of the key reaches it. The unit's own Environment= and UnsetEnvironment= lines,
  # and nothing more: a drop-in could unset WEBSPEC_REQUIRE_GUARD_KEY, or set it to 0 after a
  # value that merely contains WEBSPEC_REQUIRE_GUARD_KEY=1.
  value="$(prop "$UNIT" Environment)"
  # The plain reason first; the exact comparison is what catches a decoy value that contains it.
  [[ " $value " == *" WEBSPEC_REQUIRE_GUARD_KEY=1 "* ]] || say "$UNIT does not set WEBSPEC_REQUIRE_GUARD_KEY=1"
  expect="$(unit_lines Environment)"
  [[ "$value" == "$expect" ]] || say "$UNIT sets Environment=$value, not only its own: $expect"
  value="$(prop "$UNIT" UnsetEnvironment)"
  for p in WEBSPEC_GUARD_KEY_FILE WEBSPEC_GUARD_KEY_DEV_EPHEMERAL; do
    [[ " $value " == *" $p "* ]] || say "$UNIT does not unset $p"
  done
  expect="$(unit_lines UnsetEnvironment)"
  [[ "$value" == "$expect" ]] || say "$UNIT has UnsetEnvironment=$value, not only its own: $expect"
  for p in BindPaths BindReadOnlyPaths RootDirectory RootImage; do
    value="$(prop "$UNIT" "$p")"
    [[ -z "$value" ]] || say "$UNIT has $p=$value"
  done
  for p in NoNewPrivileges=yes ProtectSystem=strict ProtectHome=yes PrivateTmp=yes ProtectProc=invisible \
    CapabilityBoundingSet=; do
    value="$(prop "$UNIT" "${p%%=*}")"
    [[ "$value" == "${p#*=}" ]] || say "$UNIT has ${p%%=*}=$value, not ${p#*=}"
  done
  value="$(prop "$SOCKET" Listen)"
  [[ "$value" == "127.0.0.1:$GATEWAY_PORT (Stream)" ]] || say "$SOCKET listens on ${value//$'\n'/, }, not 127.0.0.1:$GATEWAY_PORT alone"
  for p in Accept=no ReusePort=no Triggers="$UNIT"; do
    value="$(prop "$SOCKET" "${p%%=*}")"
    [[ "$value" == "${p#*=}" ]] || say "$SOCKET has ${p%%=*}=$value, not ${p#*=}"
  done
  for p in ExecStartPre ExecStartPost ExecStopPre ExecStopPost; do
    value="$(prop "$SOCKET" "$p")"
    [[ -z "$value" ]] || say "$SOCKET runs $p=$value"
  done
  return 0
}

# check_units stops, before anything is enabled or started, unless the installed units run
# what this script installs.
check_units() {
  local problems line
  if ((DRY)); then
    say "# check: the units as systemd runs them, drop-ins included, run $VENV/bin/python -I -m webspec as $SVC_USER after the guard-key check, read only $ENV_FILE and listen on 127.0.0.1:$GATEWAY_PORT only; no drop-ins in /etc or /run (or --allow-drop-ins); the gateway requires a usable WEBSPEC_GUARD_KEY and no other source of the key reaches it"
    return 0
  fi
  problems="$(effective_unit_problems)"
  [[ -n "$problems" ]] || return 0
  err "the installed units, as systemd would run them, are not what this installer ships (DP-1, DP-4):"
  while IFS= read -r line; do
    printf '  %s\n' "$line" >&2
  done <<<"$problems"
  if ((OPT_ALLOW_DROP_INS)); then
    cat >&2 <<EOF
--allow-drop-ins admits drop-ins you have reviewed, but not these: no drop-in may change who
runs what, the environment, the sandbox or the listener. Variables for the gateway belong in
$ENV_FILE. Remove or fix the drop-ins (in /etc/systemd/system/ or /run/systemd/: $UNIT.d/,
$SOCKET.d/, service.d/, socket.d/, and those that systemctl set-property wrote in
system.control/), run sudo systemctl daemon-reload and this installer again. Nothing was
enabled or started.
EOF
  else
    cat >&2 <<EOF
Drop-ins outlive the unit files: one left by an earlier setup could, say, read a login
user's file after gateway.env and so choose the guard key (GD-5). Remove the drop-ins (in
/etc/systemd/system/ or /run/systemd/: $UNIT.d/, $SOCKET.d/, service.d/, socket.d/, and
those that systemctl set-property wrote in system.control/), run sudo systemctl daemon-reload
and this installer again; or keep drop-ins you have reviewed with --allow-drop-ins, under
which every other check above still applies. Nothing was enabled or started.
EOF
  fi
  exit 1
}

# socket_directives FILE prints FILE as systemd reads it, without comments, blank lines and
# surrounding blanks.
socket_directives() {
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^[#;]/d' -e '/^$/d' "$1"
}

# socket_changed INSTALLED SHIPPED succeeds when the installed socket unit is missing or has
# other directives than the shipped one: only then must the port be bound anew. Comments do
# not count.
socket_changed() {
  [[ -f "$1" ]] || return 0
  [[ "$(socket_directives "$1")" != "$(socket_directives "$2")" ]]
}

# --- Holding the port and starting (DP-9, GD-5) -----------------------------------------

# systemd_holds_port succeeds when webspec-gateway.socket is active and, where ss can tell,
# systemd (PID 1) is what listens on 127.0.0.1:$GATEWAY_PORT.
systemd_holds_port() {
  local listeners
  systemctl is-active --quiet "$SOCKET" || return 1
  have ss || return 0
  listeners="$(ss -Hltnp "sport = :$GATEWAY_PORT")" || return 1
  grep -qE "[[:space:]]127\.0\.0\.1:${GATEWAY_PORT}[[:space:]].*\"systemd\",pid=1," <<<"$listeners"
}

# port_in_use succeeds when something listens on the port now (the socket, a gateway that
# binds it itself, any other process), so that binding it anew first frees it.
port_in_use() {
  systemctl is-active --quiet "$SOCKET" && return 0
  systemctl is-active --quiet "$UNIT" && return 0
  have ss && [[ -n "$(ss -Hltn "sport = :$GATEWAY_PORT" 2>/dev/null)" ]]
}

unit_failed() {
  journalctl -u "$SOCKET" -u "$UNIT" -n 30 --no-pager >&2 || true
  die "$*"
}

# bind_failed TUNNEL_STOPPED: systemd could not bind the port. Shows who holds it and stops.
bind_failed() {
  ss -Hltnp "sport = :$GATEWAY_PORT" >&2 || true
  journalctl -u "$SOCKET" -n 20 --no-pager >&2 || true
  if (($1)); then
    err "$TUNNEL was stopped for the switch and stays stopped, so whatever holds the port gets none of the tunnel's traffic. Start it again once this installer has succeeded: sudo systemctl start $TUNNEL"
  fi
  die "systemd could not bind 127.0.0.1:$GATEWAY_PORT ($SOCKET). If another process holds it (above), stop that process and run this installer again."
}

# hold_port enables webspec-gateway.socket and leaves 127.0.0.1:$GATEWAY_PORT held by systemd
# with the installed settings. It binds the port anew only when it must: the first time, from
# a gateway that bound it itself, or when the socket's directives changed (SOCKET_CHANGED).
# The port is then free for a moment, and any local process could take it and receive what
# Caddy forwards there (DP-9), so the tunnel is stopped until systemd holds the port.
hold_port() {
  local tunnel_stopped=0 others
  systemctl enable --quiet "$SOCKET"
  if ((!SOCKET_CHANGED)) && systemd_holds_port; then
    say "$SOCKET keeps 127.0.0.1:$GATEWAY_PORT bound."
    return 0
  fi
  if port_in_use; then
    if systemctl is-active --quiet "$TUNNEL"; then
      systemctl stop "$TUNNEL"
      tunnel_stopped=1
      say "Stopped $TUNNEL while 127.0.0.1:$GATEWAY_PORT changes hands (DP-9)."
    fi
    if have pgrep; then
      others="$(pgrep -x cloudflared | paste -sd' ' -)" || others=""
      if [[ -n "$others" ]]; then
        warn "cloudflared also runs outside $TUNNEL (PID $others), which this script cannot stop: until systemd holds 127.0.0.1:$GATEWAY_PORT again, a local process could take the port and receive that tunnel's traffic. Stop it yourself before a run that has to bind the port anew (the first one, or new socket settings)."
      fi
    fi
  fi
  systemctl stop "$UNIT" "$SOCKET"
  if ! systemctl start "$SOCKET" || ! systemd_holds_port; then
    bind_failed "$tunnel_stopped"
  fi
  say "$SOCKET holds 127.0.0.1:$GATEWAY_PORT."
  if ((tunnel_stopped)); then
    if systemctl start "$TUNNEL"; then
      say "Started $TUNNEL again."
    else
      warn "$TUNNEL did not start again; start it: sudo systemctl start $TUNNEL"
    fi
  fi
}

# http_status prints the status of GET / (Host: localhost) on 127.0.0.1:$GATEWAY_PORT, if the
# answer comes within 15 s, with bash's /dev/tcp: no curl needed, and no proxy variable in the
# way.
http_status() {
  # shellcheck disable=SC2016 # expanded by the inner bash
  timeout 15 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1" &&
    printf "GET / HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3 &&
    IFS=" " read -r _ code _ <&3 && printf "%s\n" "$code"' http_status "$GATEWAY_PORT" 2>/dev/null
}

# how_it_ended [PID] prints how the unit's main process PID ended (" with status 3", " on signal
# 9"), or nothing when systemd does not say; without PID, how the last one did. systemd
# describes a main process that ended only until it starts the next one, RestartSec later, and
# then that one, which runs (ExecMainCode=0): so the process it describes must be PID. systemd
# records the end when it reaps the process, and a moment is allowed for that.
how_it_ended() {
  local i=0 line main code status
  while :; do
    main="" code="" status=""
    while IFS= read -r line; do
      case "$line" in
        ExecMainPID=*) main="${line#*=}" ;;
        ExecMainCode=*) code="${line#*=}" ;;
        ExecMainStatus=*) status="${line#*=}" ;;
      esac
    done < <(systemctl show "$UNIT" -p ExecMainPID -p ExecMainCode -p ExecMainStatus 2>/dev/null)
    if [[ -n "${1:-}" && "$main" != "$1" ]]; then
      return 0 # systemd describes another process by now
    fi
    if [[ -z "${1:-}" || "${code:-0}" != 0 ]] || ((i >= 4)); then
      break
    fi
    i=$((i + 1))
    sleep 0.5
  done
  case "$code" in
    1) [[ -z "$status" || "$status" == 0 ]] || printf ' with status %s' "$status" ;;
    2 | 3) printf ' on signal %s' "$status" ;;
  esac
}

# gateway_exited PID: the unit's process PID is gone before it answered. Says how it ended,
# and why that may be, with the journal, and stops.
gateway_exited() {
  unit_failed "$UNIT (PID $1) exited right after it started$(how_it_ended "$1"), before it answered; systemd starts it again every few seconds, to the same end. The journal above says why. One cause: WEBSPEC_GUARD_KEY in $ENV_FILE holds no key the gateway can use, such as invisible characters only (GD-5)."
}

# main_gone PID succeeds when PID is no longer the unit's main process, or no longer runs.
main_gone() {
  [[ "$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)" != "$1" || -z "$(pid_uid "$1")" ]]
}

# verify_gateway: success means the unit's own process, running as webspec, answers on the
# socket that systemd holds. (Any local user can bind a free port: F12, DP-9.) The gateway
# answers only once it serves, and it does not serve without a guard key it can use: it exits
# first (status 3, P8), and systemd starts it again every RestartSec (3 s), under another PID,
# to the same end. So while it waits for the answer (http_status, 15 s at most), it looks once a
# second whether the process is gone, and reports one that is at once, while systemd still
# describes how it ended: by the end of the 15 s, systemd describes a later one.
verify_gateway() {
  local i=0 pid="" uid code=""
  while ((i < 40)); do
    pid="$(systemctl show -p MainPID --value "$UNIT")" || pid=""
    [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] || break
    i=$((i + 1))
    sleep 0.5
  done
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || unit_failed "$UNIT has no main process$(how_it_ended)"
  uid="$(pid_uid "$pid")" || uid=""
  # A process that is gone already crashed: say so, not that it ran as nobody.
  [[ -n "$uid" ]] || gateway_exited "$pid"
  [[ "$uid" == "$(id -u "$SVC_USER")" ]] ||
    unit_failed "$UNIT (PID $pid) runs as $(user_name "$uid"), not $SVC_USER"
  systemd_holds_port ||
    unit_failed "127.0.0.1:$GATEWAY_PORT is not held by systemd ($SOCKET): $(ss -Hltnp "sport = :$GATEWAY_PORT" 2>/dev/null || true)"
  # The answer comes on fd 4, or "none" once http_status gives up, so that a read that fails is
  # a second gone by, in bash 3.2 too (where a read that times out fails like one at the end).
  exec 4< <(
    exec 2>/dev/null
    http_status || echo none
  )
  i=0
  until IFS= read -r -t 1 code <&4; do
    if main_gone "$pid"; then
      exec 4<&-
      gateway_exited "$pid"
    fi
    i=$((i + 1))
    ((i < 20)) || break
  done
  exec 4<&-
  if [[ ! "$code" =~ ^[0-9]{3}$ ]]; then
    if main_gone "$pid"; then
      gateway_exited "$pid"
    fi
    unit_failed "$UNIT (PID $pid) does not answer HTTP on 127.0.0.1:$GATEWAY_PORT"
  fi
  say "Started $UNIT: PID $pid as $SVC_USER, on 127.0.0.1:$GATEWAY_PORT held by $SOCKET (GET / for Host localhost: HTTP $code)."
  say "Check it with: systemctl status $UNIT; journalctl -u $UNIT"
}

# start_gateway enables the gateway and (re)starts it on the socket systemd holds: restart,
# not start, so that a re-run loads the upgraded code and unit. The socket keeps the port
# bound meanwhile, so no other process can take it (DP-9).
start_gateway() {
  systemctl enable --quiet "$UNIT"
  systemctl restart "$UNIT" || unit_failed "$UNIT did not start"
  verify_gateway
}

# leave_keyless: gateway.env has no guard key, so the gateway is neither enabled nor started
# (GD-5, F17); the unit would refuse to start anyway.
leave_keyless() {
  say "Not enabling or starting $UNIT: WEBSPEC_GUARD_KEY in $ENV_FILE is empty, or holds only whitespace or invisible characters. It does not start without a key; $SOCKET holds 127.0.0.1:$GATEWAY_PORT meanwhile."
  if systemctl is-enabled --quiet "$UNIT" 2>/dev/null; then
    systemctl disable --quiet "$UNIT"
    warn "disabled $UNIT, which an earlier install enabled: it cannot start without a guard key (GD-5). A run with the key enables it again."
  fi
  if systemctl is-active --quiet "$UNIT"; then
    warn "$UNIT still runs, with the guard key it started with. Once it stops, it does not start again until $ENV_FILE has a key."
  fi
}

# plan_start prints, for a dry run, what hold_port, start_gateway and leave_keyless do.
plan_start() {
  say "# if no user-level $UNIT is running (it binds the port itself), with or without the guard key:"
  run systemctl enable --quiet "$SOCKET"
  say "#   if $SOCKET is new, its directives changed, or systemd does not hold 127.0.0.1:$GATEWAY_PORT (the port is free for a moment):"
  say "#     if something listens on the port now and $TUNNEL is running:"
  run systemctl stop "$TUNNEL"
  run systemctl stop "$UNIT" "$SOCKET"
  run systemctl start "$SOCKET"
  say "#     then check that systemd holds 127.0.0.1:$GATEWAY_PORT, and start $TUNNEL again if it was stopped:"
  run systemctl start "$TUNNEL"
  say "# if $ENV_FILE sets WEBSPEC_GUARD_KEY to more than whitespace and invisible characters:"
  run systemctl enable --quiet "$UNIT"
  say "#   restart, not start: a re-run must load the upgraded code; the socket stays bound"
  run systemctl restart "$UNIT"
  say "#   then check that $UNIT runs as $SVC_USER and answers on 127.0.0.1:$GATEWAY_PORT, held by systemd (a gateway that exits instead, as it does for a guard key it cannot use, fails the run)"
  say "# otherwise $UNIT is not enabled (one enabled earlier is disabled) or started: it does not start without the key (GD-5). The next steps are printed:"
}

# next_steps STATE prints what is left to do. STATE: running (the gateway runs), keyless (no
# guard key yet) or blocked (a user-level gateway holds the port).
next_steps() {
  local n=0
  step() {
    n=$((n + 1))
    printf '  %d. ' "$n"
    cat
  }
  printf '\nNext steps:\n'
  if [[ "$1" != running ]]; then
    step <<EOF
Put the guard key in $ENV_FILE from your password manager, without
     putting it on a command line. With the 1Password CLI, for example (a value without a
     final newline is taken too):
       op read 'op://WebSpec/gateway-guard/key' |
         sudo sh -c 'IFS= read -r k || [ -n "\$k" ] && printf "WEBSPEC_GUARD_KEY=%s\\n" "\$k" >> $ENV_FILE'
EOF
  fi
  step <<EOF
List the MCP servers in $CONFIG (the format: $CONFIG_REF).
     Keep secrets out of it: write "\${NAME}" there and put NAME=value in $ENV_FILE.
     If Caddy forwards a public domain, set WEBSPEC_DOMAIN in $ENV_FILE as well:
       printf 'WEBSPEC_DOMAIN=%s\\n' example.com | sudo /usr/bin/tee -a $ENV_FILE >/dev/null
     Edit these files as root with a fixed editor: sudo -H /usr/bin/vi $ENV_FILE
     Never with sudo -e (sudoedit): it copies the file, guard key included, to a file of
     your own user, the one the agent may share, and runs your editor with your environment.
     After changing $ENV_FILE: sudo systemctl restart $UNIT
     ($CONFIG is re-read every 30 seconds.)
EOF
  step <<EOF
Add the level-4 approvers' public keys to $SIGNERS.
EOF
  case "$1" in
    keyless)
      step <<EOF
Enable and start the gateway once the key is in place, or run this installer again:
       sudo systemctl enable --now $UNIT
     $SOCKET holds 127.0.0.1:$GATEWAY_PORT already. Until the key is there, a
     connection to it only makes $UNIT fail its key check, retried every few seconds.
EOF
      ;;
    blocked)
      step <<EOF
Stop the user-level gateway as its user (systemctl --user disable --now $UNIT), then
     run this installer again: systemd then holds 127.0.0.1:$GATEWAY_PORT for the gateway.
EOF
      ;;
  esac
  step <<EOF
Put Caddy in front of the gateway (DP-5, DP-6), forwarding only hosts under your public
     domain to 127.0.0.1:$GATEWAY_PORT:
       sudo $GATEWAY_SRC/tools/setup-caddy.sh
     It runs Caddy on 127.0.0.1:7001 and [::1]:7001, sockets held by systemd. Then point
     cloudflared at Caddy.
EOF
  step <<EOF
DP-3 is not set up here: confining the agent's network egress so that it reaches only
     Caddy (127.0.0.1:7001 and [::1]:7001) is the operator's job; see the deployment guide,
     $GUIDE
EOF
}

# Sourcing defines the functions without running anything (the tests use this).
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

parse_args "$@"
readonly DRY OPT_PYTHON OPT_ALLOW_NONROOT_PYTHON OPT_ALLOW_EXISTING_USER OPT_ALLOW_DROP_INS

# --- Preflight ------------------------------------------------------------------------
# A real run without root also reports an untrusted checkout, so both are fixed in one go.
failed=0
if ((DRY)); then
  say "DRY RUN: the plan for a fresh host. Nothing is changed; '# if' conditions are checked on a real run."
elif ((EUID != 0)); then
  err "run as root (sudo $0), or with --dry-run to print the plan"
  failed=1
fi
for f in "$GATEWAY_SRC/pyproject.toml" "$GATEWAY_SRC/webspec/__main__.py" \
  "$GATEWAY_SRC/deploy/config.example.json" "$HERE/$UNIT" "$HERE/$SOCKET"; do
  [[ -f "$f" ]] || die "missing $f: run this script from a WebSpec checkout"
done
[[ "$CHECKOUT" != / ]] || die "the checkout must be a directory of its own, not /"
if ((DRY)); then
  say "# check: only root can change $CHECKOUT or the directories above it (root runs this script and installs the gateway from it)"
elif ! untrusted="$(changeable_by_others)"; then
  err "cannot check who can change $CHECKOUT"
  failed=1
elif [[ -n "$untrusted" ]]; then
  refuse_checkout "$untrusted"
  failed=1
fi
((!failed)) || exit 1
need "systemd is the init system (/run/systemd/system exists)" test -d /run/systemd/system
need "useradd, groupadd, runuser and systemctl are installed" have useradd groupadd runuser systemctl
need "no symlinks at $PREFIX, $VENV, $CONF_DIR and the files in it (root-owned files only: DP-1, DP-4)" \
  no_symlinks "$PREFIX" "$VENV" "$CONF_DIR" "$CONFIG" "$CONFIG_REF" "$ENV_FILE" "$SIGNERS"

# Python: checked before it is ever executed; pip: only root's configuration.
check_python
readonly PY_REAL PY_VERSION
check_pip_config
# The service account: never one that a person, or the agent, can log in to (DP-1).
check_account
# A webspec-gateway.service this script did not install (DP-1, DP-4).
check_foreign_unit
want "ssh-keygen is installed (it verifies level-4 approvals)" have ssh-keygen
USER_UNITS="$(running_user_units)"
if ((DRY)); then
  say "# check: warn when a user-level $UNIT is running (it listens on the same port)"
fi
if [[ -n "$USER_UNITS" ]]; then
  warn "a user-level $UNIT is running (${USER_UNITS//$'\n'/, }). It listens on the same port: as that user, run 'systemctl --user disable --now $UNIT' first. This script never touches it."
fi

# --- 1. Service user (DP-1) -----------------------------------------------------------
say "== Service user $SVC_USER:$SVC_GROUP"
if when "group $SVC_GROUP does not exist" no_group "$SVC_GROUP"; then
  run groupadd --system "$SVC_GROUP"
fi
if when "user $SVC_USER does not exist" no_user "$SVC_USER"; then
  run useradd --system --gid "$SVC_GROUP" --home-dir "$STATE_DIR" --no-create-home \
    --shell /usr/sbin/nologin --comment "WebSpec gateway" "$SVC_USER"
fi
if ((!DRY)); then
  members="$(getent group "$SVC_GROUP" | cut -d: -f4)"
  if [[ -n "$members" ]]; then
    warn "the group $SVC_GROUP has the members $members: they can read $CONF_DIR (config.json, allowed_signers), though not gateway.env."
  fi
fi
run install -d -m 0700 -o "$SVC_USER" -g "$SVC_GROUP" "$STATE_DIR"

# --- 2. Code: root-owned virtualenv ---------------------------------------------------
say "== Code: $VENV"
run install -d -m 0755 -o root -g root "$PREFIX"
if ((DRY)); then
  say "# if $VENV does not exist, holds a file a user other than root can change (moved aside, never run), or cannot be reused (another interpreter or Python X.Y, no working pip), a new one is built in its place; the old one waits aside until the new one has its packages and imports, and is put back if it does not:"
  run "$PY_REAL" -I -S -m venv "$VENV"
else
  # Nothing under $PREFIX runs as root unless only root can change it (DP-1, DP-4).
  if bad="$(unsafe_chain "$PREFIX")"; then
    die "$bad is $(describe "$bad"): a user other than root could swap the gateway's code under it. Fix that first."
  fi
  remove_leftovers
  others="$(find "$PREFIX" -mindepth 1 -maxdepth 1 ! -name venv \( ! -uid 0 -o -perm -020 -o -perm -002 \) -print)" ||
    others=""
  if [[ -n "$others" ]]; then
    warn "users other than root can change ${others//$'\n'/, } in $PREFIX. Keep the code of the stdio servers root-owned: they run as $SVC_USER (DP-1)."
  fi
  # F18: until venv_done, any exit puts the previous venv back.
  trap venv_rollback EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  prepare_venv "$VENV"
fi
run "$VENV/bin/python" -I -m pip --isolated install --quiet --disable-pip-version-check --upgrade "$GATEWAY_SRC"
run chown -R root:root "$VENV"
run chmod -R go-w "$VENV"
run chmod 0755 "$VENV"
# The installed code imports, as the user that runs it.
quiet runuser -u "$SVC_USER" -- /usr/bin/env -i HOME="$STATE_DIR" PATH=/usr/bin:/bin \
  "$VENV/bin/python" -I -c 'import webspec.app'
if ((!DRY)); then
  venv_done
  trap - EXIT HUP INT TERM
fi

# --- 3. Configuration: root-owned, read-only to the service (DP-4) --------------------
say "== Configuration: $CONF_DIR"
run install -d -m 0750 -o root -g "$SVC_GROUP" "$CONF_DIR"
if when "$CONFIG does not exist" missing "$CONFIG"; then
  # Not the example, whose servers would go live: a configuration with none.
  write_file 0640 root "$SVC_GROUP" "$CONFIG" <<'EOF'
{
  "_comment": [
    "WebSpec gateway configuration (WEBSPEC_CONFIG), root:webspec 0640. The gateway re-reads it every 30 seconds.",
    "Add MCP servers under mcpServers; config.example.json next to this file shows the format (docs/spec/audit-deployment.md, Configuration).",
    "Secrets: put NAME=value in /etc/webspec/gateway.env and write \"${NAME}\" here, in a stdio server's env or an http server's headers. Never in args: any local user can read a process's arguments.",
    "A stdio server runs as webspec, like the gateway: give it an absolute command, and keep its program and code root-owned and outside every user's home, for example under /opt/webspec/services.",
    "Edit this file as root with a fixed editor (sudo -H /usr/bin/vi /etc/webspec/config.json), never with sudo -e."
  ],
  "mcpServers": {}
}
EOF
fi
run install -m 0644 -o root -g root "$GATEWAY_SRC/deploy/config.example.json" "$CONFIG_REF"
if when "$ENV_FILE does not exist" missing "$ENV_FILE"; then
  write_file 0600 root root "$ENV_FILE" <<'EOF'
# /etc/webspec/gateway.env: environment of webspec-gateway.service (root:root 0600).
# systemd reads this file as root before it switches to the webspec user, so the service
# user cannot read it; the values reach only the gateway's environment, which the gateway
# closes to same-user processes at startup (DP-2). Each stdio MCP server receives only the
# variables its entry in config.json names.
# Format: one KEY=VALUE per line (systemd EnvironmentFile=, not a shell script). Values here
# override the unit's own settings: keep to WEBSPEC_DOMAIN and secrets.
#
# Edit it as root with a fixed editor (sudo -H /usr/bin/vi /etc/webspec/gateway.env), or
# append to it as root from standard input, as below. Never with sudo -e (sudoedit), which
# copies the file, guard key included, to a file of your own user, the one the agent may
# share, and runs your editor with your environment.
# Then: sudo systemctl restart webspec-gateway.service

# Public domain that Caddy forwards to the gateway, such as i-a-m.live. Leave it empty to
# serve loopback names (*.localhost) only.
WEBSPEC_DOMAIN=

# Secrets of MCP servers. config.json refers to them as "${NAME}" in a stdio server's
# "env" or an http server's "headers":
#SOME_TOKEN=

# Guard key (required, docs/spec/levels.md GD-5): the gateway does not start without it.
# Store 64 hex digits in your vault (openssl rand -hex 32): systemd drops backslashes and
# surrounding quotes from values in this file, so a passphrase containing them would reach
# the gateway changed. Fill it from the password manager without putting it on a command
# line. With the 1Password CLI, for example (the last assignment in this file wins):
#   op read 'op://WebSpec/gateway-guard/key' |
#     sudo sh -c 'IFS= read -r k || [ -n "$k" ] && printf "WEBSPEC_GUARD_KEY=%s\n" "$k" >> /etc/webspec/gateway.env'
#   sudo systemctl enable --now webspec-gateway.service
WEBSPEC_GUARD_KEY=
EOF
fi
if when "$SIGNERS does not exist" missing "$SIGNERS"; then
  write_file 0644 root root "$SIGNERS" <<'EOF'
# /etc/webspec/allowed_signers: the people who may approve level-4 requests
# (docs/spec/levels.md AP-3), in OpenSSH allowed-signers format. root-owned 0644: the
# gateway holds public keys only. While this file names nobody, requests that need an
# approval are refused with 503 approval_unavailable (AP-7). One line per approver:
#   ana@example.com namespaces="webspec-approval" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...
# Edit it as root with a fixed editor (sudo -H /usr/bin/vi /etc/webspec/allowed_signers),
# never with sudo -e: whoever can add a line here can approve level-4 requests.
EOF
fi
# Owners and modes are re-applied on every run, in case a hand edit changed them.
run chown root:"$SVC_GROUP" "$CONFIG"
run chmod 0640 "$CONFIG"
run chown root:root "$ENV_FILE"
run chmod 0600 "$ENV_FILE"
run chown root:root "$SIGNERS"
run chmod 0644 "$SIGNERS"

# --- 4. The units ---------------------------------------------------------------------
# systemd holds the gateway's port (webspec-gateway.socket), so it stays bound while the
# gateway restarts or waits for its key, and no other local process can take what Caddy
# forwards there (DP-9).
say "== Units: $SOCKET_PATH, $UNIT_PATH"
SOCKET_CHANGED=1
if ((!DRY)) && ! socket_changed "$SOCKET_PATH" "$HERE/$SOCKET"; then
  SOCKET_CHANGED=0
fi
if ((!DRY)) && [[ -n "$FOREIGN_FILE" ]]; then
  say "Saved the previous $UNIT as $(backup_unit)"
fi
run install -m 0644 -o root -g root "$HERE/$SOCKET" "$SOCKET_PATH"
run install -m 0644 -o root -g root "$HERE/$UNIT" "$UNIT_PATH"
run systemctl daemon-reload
# Drop-ins outlive the unit files: none may make the units run something else, as someone
# else, with another environment file, or listen elsewhere.
check_units

# --- 5. The port, and the gateway (DP-9, GD-5) -----------------------------------------
say "== Port 127.0.0.1:$GATEWAY_PORT ($SOCKET) and the gateway ($UNIT)"
if ((DRY)); then
  plan_start
  next_steps keyless
elif [[ -n "$USER_UNITS" ]]; then
  warn "not holding 127.0.0.1:$GATEWAY_PORT or starting $UNIT while a user-level $UNIT is running: it binds that port itself. Stop it as its user (systemctl --user disable --now $UNIT), then run this installer again."
  next_steps blocked
else
  hold_port
  if guard_key_set "$ENV_FILE"; then
    start_gateway
    next_steps running
  else
    leave_keyless
    next_steps keyless
  fi
fi
