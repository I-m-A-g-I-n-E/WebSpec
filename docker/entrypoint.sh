#!/usr/bin/env sh
set -eu
# Earlier versions of the compose stack mounted the single file WEBSPEC_CONFIG_HOST named. It
# now mounts the directory WEBSPEC_CONFIG_DIR names, and would otherwise silently serve
# docker/config/, in the checkout, instead of the protected file an upgraded stack still names
# (DP-4). Compose passes the variable on for this check only: refuse to start while it is set.
if [ -n "${WEBSPEC_CONFIG_HOST:-}" ]; then
  echo "entrypoint: WEBSPEC_CONFIG_HOST ($WEBSPEC_CONFIG_HOST) is retired and not read. Put the configuration in a directory of its own as claude.json, set WEBSPEC_CONFIG_DIR to that directory, and unset WEBSPEC_CONFIG_HOST, in docker/.env too (docker/README.md, \"Service configuration\")" >&2
  exit 1
fi
# The directory that holds the configuration file: /config in the compose stack.
case "${WEBSPEC_CONFIG:-}" in
  */*) config_dir="${WEBSPEC_CONFIG%/*}"; config_dir="${config_dir:-/}" ;;
  *) config_dir=. ;;
esac
# A WEBSPEC_CONFIG_DIR that names the file instead of its directory makes Docker mount that
# file at /config, and the gateway could never read /config/claude.json. Nothing short of
# fixing the mount would bring its services back, so refuse to start, before reading any
# secret, as for WEBSPEC_CONFIG_HOST.
if [ -n "${WEBSPEC_CONFIG:-}" ] && [ -e "$config_dir" ] && [ ! -d "$config_dir" ]; then
  echo "entrypoint: $config_dir is not a directory, so $WEBSPEC_CONFIG can never be read. WEBSPEC_CONFIG_DIR must name the directory that holds claude.json, not the file (docker/README.md, \"Service configuration\")" >&2
  exit 1
fi
# Source the guard key from the password manager if not already provided. Descriptor 3 is the
# gateway's listening socket when the init passes one (docker/init.py --listen): only the
# gateway may accept on it, so op does not inherit it.
if [ -z "${WEBSPEC_GUARD_KEY:-}" ] && [ -n "${OP_GUARD_KEY_REF:-}" ]; then
  if command -v op >/dev/null 2>&1; then
    WEBSPEC_GUARD_KEY="$(op read "$OP_GUARD_KEY_REF" 3<&-)"
    export WEBSPEC_GUARD_KEY
  else
    echo "entrypoint: 'op' CLI not found but OP_GUARD_KEY_REF is set" >&2
    exit 1
  fi
fi
# The compose stack mounts a directory at /config. When the gateway cannot read the file in
# it, it starts with no services and says only that, so say why and what brings them back.
# The gateway looks at the file every 30 seconds and reads it again when it changes, chmod and
# chown included (webspec/config.py, ServiceRegistry). Containers without the directory, like
# the audit-log ones in docker/README.md, stay quiet.
if [ -n "${WEBSPEC_CONFIG:-}" ] && [ -d "$config_dir" ] && [ ! -r "$WEBSPEC_CONFIG" ]; then
  if [ ! -x "$config_dir" ]; then
    echo "entrypoint: cannot search $config_dir; no services until uid $(id -u) can (docker/README.md, \"Service configuration\")" >&2
  elif [ -e "$WEBSPEC_CONFIG" ]; then
    echo "entrypoint: cannot read $WEBSPEC_CONFIG; no services. Make it readable by uid $(id -u); the gateway picks it up within 30 seconds (docker/README.md, \"Service configuration\")" >&2
  else
    echo "entrypoint: cannot find $WEBSPEC_CONFIG; no services until it appears (docker/README.md, \"Service configuration\")" >&2
  fi
fi
exec "$@"
