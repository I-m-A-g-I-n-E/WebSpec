#!/usr/bin/env sh
set -eu
# Source the guard key from the password manager if not already provided.
if [ -z "${WEBSPEC_GUARD_KEY:-}" ] && [ -n "${OP_GUARD_KEY_REF:-}" ]; then
  if command -v op >/dev/null 2>&1; then
    WEBSPEC_GUARD_KEY="$(op read "$OP_GUARD_KEY_REF")"
    export WEBSPEC_GUARD_KEY
  else
    echo "entrypoint: 'op' CLI not found but OP_GUARD_KEY_REF is set" >&2
    exit 1
  fi
fi
exec "$@"
