#!/usr/bin/env bash
#
# build-caddy.sh: build, for CI, the Caddy that gateway/tools/setup-caddy.sh deploys, so that the
# tests that run the real Caddy on webspec.caddy's output (WEBSPEC_TEST_CADDY in
# gateway/tests/test_caddy.py) run in CI. They check that the configuration forwards only
# WebSpec hosts and keeps the logs filtered (DP-5, DP-6), on loopback, with a private admin API.
#
# The versions, the Go checksums and the modules a Caddy must have are read from setup-caddy.sh,
# never written here: bumping a pin there changes the cache key below, and CI then builds and
# tests the new Caddy; a module it starts requiring, check requires too. The build is
# setup-caddy.sh's build_caddy (step 1) on a host without Go: the pinned Go, downloaded and
# checked against the script's checksum (so CI checks that checksum as well), then xcaddy and
# the rate-limit plugin, every module checked against sum.golang.org. A plugin setup-caddy.sh
# starts building with must be added to build() below by hand.
#
# Usage: build-caddy.sh key            print key=<cache key> for $GITHUB_OUTPUT
#        build-caddy.sh build OUTPUT   build Caddy at OUTPUT (Linux on amd64 or arm64)
#        build-caddy.sh check BINARY   fail unless BINARY reports the pinned Caddy and plugin, and
#                                      the modules setup-caddy.sh requires
#
# check compares what the binary prints with the pins. That is not an integrity check: any
# program that prints the same strings passes. So neither command uses a place where a user
# other than root and the caller could swap the binary (see "Where the binary lives" below).
# To build one by hand: out="$(mktemp -d)/caddy"; build-caddy.sh build "$out"
# Tests: .github/scripts/test_build_caddy.py (python -m pytest -q .github/scripts).
#
set -euo pipefail

die() {
    echo "build-caddy: $*" >&2
    exit 1
}

# ── Where the binary lives ──
# DP-1 puts the agent on the same host as another user. If it could change the directory that
# holds the binary, or one above it, it could swap in its own program, which would run as
# whoever runs the binary next: the operator running the tests, or root if it went on to
# setup-caddy.sh. So build writes, and check runs, a binary only where no other user can.
CALLER_UID="$(id -u)"

# changeable PATH [above]: whether a user other than root and the caller can change PATH itself
# (a link is not followed). PATH must be root's or the caller's and not writable by its group or
# others; with "above" (PATH is a directory on the way to the binary), it may be writable by
# others if it is sticky, like /tmp, as no other user can then rename or remove what it holds.
# A link counts by its owner alone, its mode bits mean nothing. Owners and mode bits only: on
# Linux an ACL that grants write shows in the group bits. (-perm -020 -o -perm -002: group- or
# world-writable, and -perm -1000: sticky, in forms every find accepts.)
changeable() {
    local hit
    if [ -L "$1" ]; then
        hit="$(find "$1" -maxdepth 0 ! -uid 0 ! -uid "$CALLER_UID" -print 2>/dev/null)" || return 0
    elif [ "${2:-}" = above ]; then
        hit="$(find "$1" -maxdepth 0 \( \( ! -uid 0 ! -uid "$CALLER_UID" \) \
            -o \( \( -perm -020 -o -perm -002 \) ! -perm -1000 \) \) -print 2>/dev/null)" || return 0
    else
        hit="$(find "$1" -maxdepth 0 \( \( ! -uid 0 ! -uid "$CALLER_UID" \) -o -perm -020 -o -perm -002 \) \
            -print 2>/dev/null)" || return 0
    fi
    [ -n "$hit" ]
}

# unsafe_dir DIR [above]: the first directory or link that a user other than root and the caller
# can change on the way to DIR (absolute), or that cannot be examined; nothing if there is none.
# The path is followed as the kernel follows it, link by link, so every directory it passes
# through and every link it reads counts, also those that a link's target leads through. DIR
# holds the binary, so it may not be shared even if sticky, unless "above" (it is to hold the
# directory that will).
unsafe_dir() {
    local current=/ rest="${1#/}" name next target links=0
    case "$1" in
        /*) ;;
        *) # relative: whoever chooses the working directory chooses the binary
            printf '%s\n' "$1"
            return 0
            ;;
    esac
    if changeable / above; then
        printf '/\n'
        return 0
    fi
    while [ -n "$rest" ]; do
        name="${rest%%/*}"
        case "$rest" in
            */*) rest="${rest#*/}" ;;
            *) rest="" ;;
        esac
        case "$name" in
            "" | .) continue ;;
            ..)
                current="$(dirname -- "$current")"
                continue
                ;;
        esac
        next="${current%/}/${name}"
        if [ -L "$next" ]; then
            links=$((links + 1))
            target="$(readlink -- "$next")" || target=""
            if [ "$links" -gt 40 ] || [ -z "$target" ] || changeable "$next"; then
                printf '%s\n' "$next"
                return 0
            fi
            case "$target" in
                /*) current=/ ;; # else relative to the link's directory, which is $current
            esac
            rest="${target#/}${rest:+/${rest}}"
            continue
        fi
        if changeable "$next" above; then # also when it is missing (find fails)
            printf '%s\n' "$next"
            return 0
        fi
        current=$next
    done
    if changeable "$current" "${2:-}"; then
        printf '%s\n' "$current"
    fi
}

# refuse WHAT PATH: stop, because a user other than root and the caller can change PATH (or it
# cannot be examined).
refuse() {
    local listing mode owner
    listing="$(ls -ldn -- "$2" 2>/dev/null)" || die "refusing to ${1}: ${2} cannot be examined"
    read -r mode _ owner _ <<<"$listing" # the mode and the owner's uid, before the name
    die "refusing to ${1}: a user other than root and you (uid ${CALLER_UID}) can change ${2}" \
        "(${mode}, uid ${owner}), so they could swap the binary for their own program. Use a" \
        "directory that only you can write, below directories that only root or you can write" \
        "(or sticky ones, like /tmp): out=\"\$(mktemp -d)/caddy\""
}

SETUP_SCRIPT="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)/gateway/tools/setup-caddy.sh"
[ -f "$SETUP_SCRIPT" ] || die "missing ${SETUP_SCRIPT}"

# setting NAME PATTERN: the value setup-caddy.sh gives NAME on its line NAME="${VAR:-value}" or
# NAME=value (quotes optional, a comment may follow). There must be exactly one such line, and
# the value must match PATTERN (an ERE).
setting() {
    local value
    value="$(sed -nE -e 's/^'"$1"'="?[$][{][A-Z_]+:-([^}"]+)[}]"?([[:space:]]+#.*)?$/\1/p' \
        -e 's/^'"$1"'=([^"$#[:space:]]+)([[:space:]]+#.*)?$/\1/p' "$SETUP_SCRIPT")"
    case "$value" in
        "" | *$'\n'*) die "expected one line ${1}=\"\${...:-value}\" in ${SETUP_SCRIPT}" ;;
    esac
    grep -Eqx -- "$2" <<<"$value" || die "unexpected ${1}=${value} in ${SETUP_SCRIPT}"
    printf '%s\n' "$value"
}

VERSION='v[0-9]+[.][0-9]+[.][0-9]+([-+][0-9A-Za-z.-]+)?'
CADDY_VERSION="$(setting CADDY_VERSION "$VERSION")"
RATELIMIT_VERSION="$(setting RATELIMIT_VERSION "$VERSION")"
XCADDY_VERSION="$(setting XCADDY_VERSION "$VERSION")"
GO_VERSION="$(setting GO_VERSION '[0-9]+([.][0-9]+)+((rc|beta)[0-9]+)?')"
RATELIMIT_PLUGIN="$(setting RATELIMIT_PLUGIN '[a-z0-9.-]+(/[A-Za-z0-9._~-]+)+')"

# The modules that setup-caddy.sh requires of the Caddy it installs (step 1), from its one loop
# "for module in MODULE...; do" (a comment may follow). check requires the same, so a module
# required there is required of CI's Caddy too.
loop='^[[:space:]]*for[[:space:]]+module[[:space:]]+in'
loops="$(grep -cE -- "${loop}([[:space:]]|\$)" "$SETUP_SCRIPT")" || true # grep -c fails on none
[ "$loops" = 1 ] || die "expected one loop \"for module in MODULE...; do\" in ${SETUP_SCRIPT}, found ${loops:-none}"
REQUIRED_MODULES=()
read -r -a REQUIRED_MODULES <<<"$(sed -nE "s/${loop}[[:space:]]+([^;#]*);[[:space:]]*do([[:space:]]+#.*)?\$/\\1/p" \
    "$SETUP_SCRIPT")" # split on blanks, never globbed
[ "${#REQUIRED_MODULES[@]}" -gt 0 ] \
    || die "expected the modules on one line \"for module in MODULE...; do\" in ${SETUP_SCRIPT}"
for module in "${REQUIRED_MODULES[@]}"; do
    grep -Eqx -- '[A-Za-z0-9_-]+([.][A-Za-z0-9_-]+)*' <<<"$module" \
        || die "unexpected module ${module} in the loop \"for module in\" of ${SETUP_SCRIPT}"
done

case "$(uname -m)" in
    x86_64) GOARCH_NAME=amd64 ;;
    aarch64 | arm64) GOARCH_NAME=arm64 ;;
    *) die "no Go download for $(uname -m)" ;;
esac

# The checksum that setup-caddy.sh's go_sha256() gives the Go archive for this machine, from its
# line "VERSION-ARCH) echo SHA256 ;;".
sum_line="^[[:space:]]*${GO_VERSION//./[.]}-${GOARCH_NAME}[)][[:space:]]*echo[[:space:]]+([0-9a-f]{64})[[:space:]]*;;"
GO_SHA256="$(sed -nE "s/${sum_line}([[:space:]]*#.*)?\$/\\1/p" "$SETUP_SCRIPT")"
case "$GO_SHA256" in
    "" | *$'\n'*) die "expected one checksum for Go ${GO_VERSION} linux-${GOARCH_NAME} in ${SETUP_SCRIPT}" ;;
esac

key() {
    local digest
    # Everything that decides the binary or what its build verifies: the pins, the Go checksum (an
    # edited checksum is downloaded and checked again) and this script. Not the modules: check
    # runs on every run, on a cached binary too.
    digest="$({
        printf '%s\n' "$CADDY_VERSION" "$RATELIMIT_PLUGIN" "$RATELIMIT_VERSION" "$XCADDY_VERSION" \
            "$GO_VERSION" "$GO_SHA256"
        cat -- "${BASH_SOURCE[0]}"
    } | sha256sum)"
    echo "Caddy ${CADDY_VERSION} with ${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}, xcaddy ${XCADDY_VERSION}," \
        "Go ${GO_VERSION} (the pins in ${SETUP_SCRIPT})" >&2
    echo "Modules setup-caddy.sh requires: ${REQUIRED_MODULES[*]}" >&2
    printf 'key=caddy-%s-ratelimit-%s-xcaddy-%s-go%s-linux-%s-%s\n' "$CADDY_VERSION" "$RATELIMIT_VERSION" \
        "$XCADDY_VERSION" "$GO_VERSION" "$GOARCH_NAME" "${digest:0:16}"
}

WORK=""
cleanup() {
    if [ -n "$WORK" ]; then
        chmod -R u+w -- "$WORK" 2>/dev/null || true # the module cache is read-only
        rm -rf -- "$WORK" || true
    fi
}
trap cleanup EXIT

build() { # build OUTPUT
    local out dir parent bad go go_version
    [ "$(uname -s)" = Linux ] || die "the build downloads Go for Linux; run it on Linux"
    case "$1" in
        /*) out="$1" ;;
        *) out="${PWD}/$1" ;;
    esac
    # Nothing is created through a directory another user can change (DP-1): check the deepest
    # one that exists, create the rest private to the caller, then check the whole path.
    dir="$(dirname -- "$out")"
    parent=$dir
    while [ ! -e "$parent" ] && [ ! -L "$parent" ]; do parent="$(dirname -- "$parent")"; done
    if [ "$parent" != "$dir" ]; then
        bad="$(unsafe_dir "$parent" above)"
        [ -z "$bad" ] || refuse "build into ${dir}" "$bad"
        (umask 077 && mkdir -p -- "$dir")
    fi
    bad="$(unsafe_dir "$dir")"
    [ -z "$bad" ] || refuse "build into ${dir}" "$bad"
    [ -d "$dir" ] || die "${dir} is not a directory"
    WORK="$(mktemp -d)"

    # As setup-caddy.sh: no go env file, module cache or build cache from a home directory, and
    # every module checked against the Go checksum database. Also cleared here: GOPROXY and GOSUMDB,
    # so nothing in the environment can turn that check off, and GOROOT, GOOS and GOARCH, which
    # would pair the new go command with another toolchain or target. CGO_ENABLED=0 (setup-caddy.sh
    # leaves the default) makes a static binary, which a later runner image can run from the cache.
    export GOENV=off GOPATH="${WORK}/gopath" GOCACHE="${WORK}/gocache" TMPDIR="${WORK}/tmp" GOTOOLCHAIN=auto
    export CGO_ENABLED=0
    unset GOFLAGS GONOSUMDB GONOSUMCHECK GOINSECURE GOPRIVATE GONOPROXY GOPROXY GOSUMDB GOROOT GOOS GOARCH
    mkdir -p -- "$TMPDIR"

    echo "Downloading Go ${GO_VERSION} (linux-${GOARCH_NAME}), to check against setup-caddy.sh's checksum..."
    curl -q -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "${WORK}/go.tar.gz" \
        "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH_NAME}.tar.gz"
    printf '%s  %s\n' "$GO_SHA256" "${WORK}/go.tar.gz" | sha256sum -c --quiet - \
        || die "the Go ${GO_VERSION} archive does not match setup-caddy.sh's checksum"
    tar -C "$WORK" -xzf "${WORK}/go.tar.gz"
    go="${WORK}/go/bin/go"
    PATH="${WORK}/go/bin:${PATH}"
    export PATH
    go_version="$("$go" version)" || die "the downloaded Go does not run"
    echo "Go: ${go_version}"

    echo "Building Caddy ${CADDY_VERSION} with ${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION} (xcaddy ${XCADDY_VERSION})..."
    GOBIN="${WORK}/bin" "$go" install "github.com/caddyserver/xcaddy/cmd/xcaddy@${XCADDY_VERSION}"
    (cd -- "$WORK" && XCADDY_SETCAP=0 "${WORK}/bin/xcaddy" build "$CADDY_VERSION" \
        --with "${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}" --output "$out")
    chmod 0755 -- "$out" # whatever the umask: not writable by group or others (check refuses that)
}

check() { # check BINARY
    local bin bad version modules module info
    case "$1" in
        /*) bin="$1" ;;
        *) bin="${PWD}/$1" ;; # never a name looked up on PATH
    esac
    [ ! -L "$bin" ] || die "${bin} is a symbolic link; give the path of the binary itself"
    if [ ! -f "$bin" ] || [ ! -x "$bin" ]; then die "${bin} is not an executable file"; fi
    # A binary another user could have swapped is not run at all, not even to ask its version.
    if changeable "$bin"; then bad=$bin; else bad="$(unsafe_dir "$(dirname -- "$bin")")"; fi
    [ -z "$bad" ] || refuse "run ${bin}" "$bad"
    version="$("$bin" version)" || die "${bin} version failed"
    [ "${version%% *}" = "$CADDY_VERSION" ] || die "${bin} is Caddy ${version}, not ${CADDY_VERSION}"
    # The modules that setup-caddy.sh requires (read from it above).
    modules="$("$bin" list-modules)" || die "${bin} list-modules failed"
    for module in "${REQUIRED_MODULES[@]}"; do
        grep -qx -- "$module" <<<"$modules" || die "${bin} lacks the module ${module}"
    done
    info="$("$bin" build-info)" || die "${bin} build-info failed"
    awk -F '\t' -v path="$RATELIMIT_PLUGIN" -v version="$RATELIMIT_VERSION" \
        '$1 == "dep" && $2 == path && $3 == version { found = 1 } END { exit !found }' <<<"$info" \
        || die "${bin} was not built with ${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}"
    echo "${bin}: Caddy ${version}, with ${RATELIMIT_PLUGIN}@${RATELIMIT_VERSION}"
}

case "${1:-}" in
    key) [ "$#" -eq 1 ] || die "usage: $0 key"; key ;;
    build) [ "$#" -eq 2 ] || die "usage: $0 build OUTPUT"; build "$2" ;;
    check) [ "$#" -eq 2 ] || die "usage: $0 check BINARY"; check "$2" ;;
    *) die "usage: $0 key | build OUTPUT | check BINARY" ;;
esac
