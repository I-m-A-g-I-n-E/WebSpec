"""End-to-end UFO tests against the live gateway. Run with: python tests/test_ufo_e2e.py"""

import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.error

BASE = "http://op-auth.localhost:7001"
HOST = "op-auth.localhost:7001"


def load_key():
    """Load the session key from the environment variable or derive it."""
    raw = os.environ.get("WEBSPEC_GUARD_KEY")
    if not raw:
        raise RuntimeError(
            "WEBSPEC_GUARD_KEY is not set. Source it from your password manager, e.g. "
            "`export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')`. "
            "For local dev only, set WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1."
        )
    # Import the derivation function from config
    from webspec.config import _derive_guard_key
    return _derive_guard_key(raw)


def guard_hmac(key, method, host, path, nonce, body=b""):
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}:{host}:{path}:{nonce}:{body_hash}".encode()
    mac = hmac.new(key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def clearance_token(key, tool, args, ts):
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    message = f"ufo:{tool}:{canonical}:{ts}".encode()
    mac = hmac.new(key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def get_nonce(key):
    mac = guard_hmac(key, "GET", HOST, "/__nonce", "", b"")
    r = urllib.request.Request(BASE + "/__nonce")
    r.add_header("X-WebSpec-Guard", mac)
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())["nonce"]


def guarded_get(key, path, tool_name=None, tool_args=None):
    nonce = get_nonce(key)
    mac = guard_hmac(key, "GET", HOST, path, nonce, b"")
    r = urllib.request.Request(BASE + path)
    r.add_header("X-WebSpec-Guard", mac)
    r.add_header("X-WebSpec-Nonce", nonce)
    # Add UFO clearance if provided
    if tool_name:
        ts = str(int(time.time()))
        ct = clearance_token(key, tool_name, tool_args or {}, ts)
        r.add_header("X-UFO-Clearance", f"{ct}:{ts}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


if __name__ == "__main__":
    key = load_key()
    print("=" * 60)
    print("UFO E2E TESTS")
    print("=" * 60)

    # 1. Open tier (list_vaults) — no clearance needed
    print("\n1. Open tier: list_vaults (no clearance)")
    status, data = guarded_get(key, "/list_vaults")
    assert status not in (401, 403), f"Guard failed: {status} {data}"
    print(f"   PASS: status={status}")

    # 2. list_vaults with clearance header (should still work)
    print("\n2. Open tier: list_vaults with clearance (still works)")
    status, data = guarded_get(key, "/list_vaults", "list_vaults", {})
    assert status not in (401, 403), f"Guard failed: {status} {data}"
    print(f"   PASS: status={status}")

    # 3. /__challenge endpoint
    print("\n3. Challenge endpoint")
    nonce_mac = guard_hmac(key, "GET", HOST, "/__challenge", "", b"")
    r = urllib.request.Request(BASE + "/__challenge?tool=run")
    r.add_header("X-WebSpec-Guard", nonce_mac)
    try:
        with urllib.request.urlopen(r) as resp:
            cdata = json.loads(resp.read())
        print(f"   PASS: challenge={cdata['challenge'][:12]}... tool={cdata['tool']}")
    except urllib.error.HTTPError as e:
        print(f"   Status: {e.code} (challenge endpoint)")

    print("\n" + "=" * 60)
    print("UFO E2E TESTS COMPLETE")
    print("=" * 60)
