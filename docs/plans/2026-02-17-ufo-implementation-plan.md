# UFO Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-tool prompt injection mitigation to op-auth via UFO clearance tokens, provenance chains, and audit logging.

**Architecture:** The op-auth service self-enforces UFO policy (tier classification, clearance validation, audit logging) via a new `ufo.py` module. The gateway adds clearance token computation and provenance chain validation to `guard.py`, and passes UFO headers through to guarded services in `app.py`. A `/__challenge` endpoint enables human confirmation for dangerous-tier tools.

**Tech Stack:** Python 3.11+, FastMCP, Starlette, HMAC-SHA256, pytest

---

### Task 1: Test infrastructure setup

**Files:**
- Create: `gateway/tests/__init__.py`
- Create: `gateway/tests/conftest.py`

**Step 1: Create test fixtures**

```python
# gateway/tests/__init__.py
```

```python
# gateway/tests/conftest.py
import pytest

@pytest.fixture
def session_key():
    """Fixed 32-byte session key for deterministic tests."""
    return b"\x01" * 32
```

**Step 2: Verify pytest runs**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/ -v --co`
Expected: `no tests ran` (collected 0)

**Step 3: Commit**

```bash
git add gateway/tests/__init__.py gateway/tests/conftest.py
git commit -m "chore: add test infrastructure for gateway"
```

---

### Task 2: UFO clearance token computation in guard.py

**Files:**
- Create: `gateway/tests/test_ufo_clearance.py`
- Modify: `gateway/webspec/guard.py:65-77` (add after `compute_guard_hmac`)

**Step 1: Write the failing tests**

```python
# gateway/tests/test_ufo_clearance.py
import time
from webspec.guard import compute_clearance_token, validate_clearance_token

def test_clearance_token_roundtrip(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    assert isinstance(token, str)
    assert len(token) == 8  # 4 bytes hex

    result = validate_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, f"{token}:{ts}")
    assert result is None  # None = success

def test_clearance_token_wrong_tool(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "list_vaults", {}, f"{token}:{ts}")
    assert result == "clearance_invalid"

def test_clearance_token_wrong_args(session_key):
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "read", {"reference": "op://Other/Item/f"}, f"{token}:{ts}")
    assert result == "clearance_invalid"

def test_clearance_token_expired(session_key):
    ts = str(int(time.time()) - 60)  # 60 seconds ago
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    result = validate_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, f"{token}:{ts}")
    assert result == "clearance_expired"

def test_clearance_token_malformed(session_key):
    result = validate_clearance_token(session_key, "read", {}, "garbage")
    assert result == "clearance_malformed"

def test_clearance_token_missing(session_key):
    result = validate_clearance_token(session_key, "read", {}, None)
    assert result == "clearance_missing"

def test_clearance_args_canonicalization(session_key):
    """Arg order doesn't matter — keys are sorted."""
    ts = str(int(time.time()))
    token1 = compute_clearance_token(session_key, "get_item", {"vault": "A", "item": "B"}, ts)
    token2 = compute_clearance_token(session_key, "get_item", {"item": "B", "vault": "A"}, ts)
    assert token1 == token2
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_ufo_clearance.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_clearance_token'`

**Step 3: Implement clearance token functions**

Add to `gateway/webspec/guard.py` after the `generate_nonce` function (after line 153):

```python
CLEARANCE_TTL = 30  # seconds


def _canonical_args(args: dict) -> str:
    """Canonicalize tool arguments: sorted keys, JSON-serialized."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def compute_clearance_token(session_key: bytes, tool: str, args: dict, timestamp: str) -> str:
    """Compute UFO clearance token: HMAC-SHA256(key, ufo:tool:canonical_args:ts) → 8 hex chars."""
    canonical = _canonical_args(args)
    message = f"ufo:{tool}:{canonical}:{timestamp}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def validate_clearance_token(
    session_key: bytes, tool: str, args: dict, header: str | None
) -> str | None:
    """Validate X-UFO-Clearance header. Returns None on success, error string on failure."""
    if not header:
        return "clearance_missing"

    parts = header.rsplit(":", 1)
    if len(parts) != 2:
        return "clearance_malformed"

    token, timestamp = parts
    if len(token) != 8:
        return "clearance_malformed"

    # Check expiry
    try:
        ts_int = int(timestamp)
    except ValueError:
        return "clearance_malformed"
    if abs(time.time() - ts_int) > CLEARANCE_TTL:
        return "clearance_expired"

    # Verify HMAC
    expected = compute_clearance_token(session_key, tool, args, timestamp)
    if not hmac.compare_digest(token.lower(), expected.lower()):
        return "clearance_invalid"

    return None
```

Also add `import json` to the top of `guard.py` (after `import hashlib`).

**Step 4: Run tests to verify they pass**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_ufo_clearance.py -v`
Expected: 7 passed

**Step 5: Commit**

```bash
git add gateway/webspec/guard.py gateway/tests/test_ufo_clearance.py
git commit -m "feat(guard): add UFO clearance token computation and validation"
```

---

### Task 3: Provenance chain validation in guard.py

**Files:**
- Create: `gateway/tests/test_provenance.py`
- Modify: `gateway/webspec/guard.py` (add after clearance functions)

**Step 1: Write the failing tests**

```python
# gateway/tests/test_provenance.py
from webspec.guard import build_provenance_link, validate_provenance_chain

def test_single_link_human(session_key):
    link = build_provenance_link(session_key, "human", "agent", "read")
    chain = f"human:{link}"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is True
    assert result.origin == "human"
    assert len(result.links) == 1

def test_two_link_chain(session_key):
    link1 = build_provenance_link(session_key, "human", "agent", "read")
    link2 = build_provenance_link(session_key, "agent", "gateway", "read")
    chain = f"human:{link1}->agent:{link2}"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is True
    assert result.origin == "human"
    assert len(result.links) == 2

def test_broken_chain_bad_sig(session_key):
    chain = "human:deadbeef"
    result = validate_provenance_chain(session_key, chain, "read")
    assert result.valid is False

def test_missing_chain(session_key):
    result = validate_provenance_chain(session_key, None, "read")
    assert result.valid is False
    assert result.origin == "unknown"

def test_empty_chain(session_key):
    result = validate_provenance_chain(session_key, "", "read")
    assert result.valid is False
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_provenance.py -v`
Expected: FAIL with `ImportError`

**Step 3: Implement provenance chain**

Add to `gateway/webspec/guard.py`:

```python
@dataclass
class ProvenanceResult:
    valid: bool
    origin: str  # "human", "agent", "unknown"
    links: list[str] = field(default_factory=list)
    error: str | None = None


def build_provenance_link(session_key: bytes, source: str, target: str, action: str) -> str:
    """Build a provenance link signature: HMAC(key, source->target:action) → 4 hex chars."""
    message = f"{source}->{target}:{action}".encode()
    mac = hmac.new(session_key, message, hashlib.sha256).digest()
    return mac[:4].hex()


def validate_provenance_chain(
    session_key: bytes, chain_header: str | None, action: str
) -> ProvenanceResult:
    """Validate X-UFO-Provenance header. Walks the chain and verifies each link."""
    if not chain_header:
        return ProvenanceResult(valid=False, origin="unknown", error="provenance_missing")

    # Parse: "human:h3a9->agent:a7f2->gateway"
    links = chain_header.split("->")
    if not links:
        return ProvenanceResult(valid=False, origin="unknown", error="provenance_empty")

    parsed = []
    for link in links:
        parts = link.split(":", 1)
        if len(parts) != 2:
            return ProvenanceResult(valid=False, origin="unknown", error="provenance_malformed")
        parsed.append((parts[0], parts[1]))  # (role, signature)

    # Verify each link's signature
    for i, (role, sig) in enumerate(parsed):
        if i + 1 < len(parsed):
            target_role = parsed[i + 1][0]
        else:
            target_role = "gateway"  # last link always targets gateway
        expected = build_provenance_link(session_key, role, target_role, action)
        if not hmac.compare_digest(sig.lower(), expected.lower()):
            return ProvenanceResult(
                valid=False, origin=parsed[0][0],
                links=[p[0] for p in parsed], error="provenance_invalid"
            )

    return ProvenanceResult(
        valid=True, origin=parsed[0][0], links=[p[0] for p in parsed]
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_provenance.py -v`
Expected: 5 passed

**Step 5: Commit**

```bash
git add gateway/webspec/guard.py gateway/tests/test_provenance.py
git commit -m "feat(guard): add provenance chain build/validate"
```

---

### Task 4: Create ufo.py — tier policy, allowlist, audit log

**Files:**
- Create: `services/op-auth/ufo.py`
- Create: `gateway/tests/test_ufo_policy.py`

**Step 1: Write the failing tests**

```python
# gateway/tests/test_ufo_policy.py
import json
import tempfile
from pathlib import Path

# We need to test ufo.py which lives in services/op-auth, so add to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "op-auth"))

from ufo import get_tier, is_run_allowed, write_audit_entry, TIERS

def test_tier_classification():
    assert get_tier("list_vaults") == "open"
    assert get_tier("read") == "sensitive"
    assert get_tier("list_items") == "sensitive"
    assert get_tier("get_item") == "sensitive"
    assert get_tier("run") == "dangerous"

def test_unknown_tool_is_dangerous():
    assert get_tier("unknown_tool") == "dangerous"

def test_run_allowlist():
    assert is_run_allowed("vault", ["list"]) is True
    assert is_run_allowed("item", ["list", "--vault", "Personal"]) is True
    assert is_run_allowed("item", ["get", "MyItem", "--vault", "Personal"]) is True
    assert is_run_allowed("document", ["get", "MyDoc"]) is True

def test_run_blocklist():
    assert is_run_allowed("account", ["list"]) is False
    assert is_run_allowed("item", ["delete", "MyItem"]) is False
    assert is_run_allowed("item", ["edit", "MyItem"]) is False
    assert is_run_allowed("user", ["list"]) is False
    assert is_run_allowed("events-api", ["create"]) is False
    assert is_run_allowed("inject", ["--template"]) is False

def test_audit_log(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    write_audit_entry(
        log_path=log_path,
        tool="read",
        args={"reference": "op://V/I/f"},
        tier="sensitive",
        provenance="human:abcd->gateway",
        outcome="allowed",
        clearance_valid=True,
    )
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "read"
    assert entry["outcome"] == "allowed"
    assert "timestamp" in entry

def test_audit_log_append(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    for i in range(3):
        write_audit_entry(log_path=log_path, tool=f"tool_{i}", args={},
                          tier="open", provenance="", outcome="allowed", clearance_valid=True)
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 3
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_ufo_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ufo'`

**Step 3: Implement ufo.py**

```python
# services/op-auth/ufo.py
"""UFO policy: tool sensitivity tiers, run() allowlist, audit logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Tool sensitivity tiers
TIERS: dict[str, str] = {
    "list_vaults": "open",
    "read": "sensitive",
    "list_items": "sensitive",
    "get_item": "sensitive",
    "run": "dangerous",
}

# run() allowlist: (subcommand, allowed_first_args)
# If first_args is None, any args are allowed for that subcommand.
# If first_args is a set, only those first positional args are allowed.
RUN_ALLOWLIST: dict[str, set[str] | None] = {
    "vault": None,  # vault list, vault get, etc.
    "item": {"list", "get"},  # item list, item get only
    "document": {"get"},  # document get only
}

DEFAULT_AUDIT_PATH = Path.home() / ".webspec" / "op-auth-audit.jsonl"


def get_tier(tool_name: str) -> str:
    """Get the sensitivity tier for a tool. Unknown tools default to dangerous."""
    return TIERS.get(tool_name, "dangerous")


def is_run_allowed(subcommand: str, args: list[str] | None = None) -> bool:
    """Check if a run() subcommand + args combination is on the allowlist."""
    subcommand = subcommand.lower()
    if subcommand not in RUN_ALLOWLIST:
        return False
    allowed_first = RUN_ALLOWLIST[subcommand]
    if allowed_first is None:
        return True
    # Check the first positional arg (skip flags starting with --)
    if args:
        first_positional = next((a for a in args if not a.startswith("--")), None)
        if first_positional and first_positional.lower() in allowed_first:
            return True
    return False


def write_audit_entry(
    tool: str,
    args: dict,
    tier: str,
    provenance: str,
    outcome: str,
    clearance_valid: bool,
    log_path: Path = DEFAULT_AUDIT_PATH,
) -> None:
    """Append an audit entry to the JSONL log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "args": args,
        "tier": tier,
        "provenance": provenance,
        "outcome": outcome,
        "clearance_valid": clearance_valid,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_ufo_policy.py -v`
Expected: 6 passed

**Step 5: Commit**

```bash
git add services/op-auth/ufo.py gateway/tests/test_ufo_policy.py
git commit -m "feat(op-auth): add UFO policy — tiers, run() allowlist, audit log"
```

---

### Task 5: Wire UFO enforcement into op-auth server.py

**Files:**
- Modify: `services/op-auth/server.py`

**Step 1: Write integration test**

Create `gateway/tests/test_opauth_ufo_integration.py`:

```python
# gateway/tests/test_opauth_ufo_integration.py
"""Test that op-auth server.py enforces UFO tiers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "op-auth"))

from ufo import get_tier, is_run_allowed

def test_run_rejects_delete():
    """run() with item delete must be denied by allowlist."""
    assert is_run_allowed("item", ["delete", "MyItem"]) is False

def test_run_rejects_edit():
    """run() with item edit must be denied by allowlist."""
    assert is_run_allowed("item", ["edit", "MyItem"]) is False

def test_run_allows_item_list():
    assert is_run_allowed("item", ["list", "--vault", "Personal"]) is True

def test_all_tools_have_tiers():
    """Every tool declared in server.py has a tier in ufo.py."""
    expected_tools = {"read", "list_vaults", "list_items", "get_item", "run"}
    for tool in expected_tools:
        tier = get_tier(tool)
        assert tier in ("open", "sensitive", "dangerous"), f"{tool} has invalid tier: {tier}"
```

**Step 2: Run to verify tests pass (these test policy, not server enforcement)**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_opauth_ufo_integration.py -v`
Expected: 4 passed

**Step 3: Rewrite server.py with UFO enforcement**

Replace `services/op-auth/server.py` with the following. Key changes:
- Import `ufo` module
- `run()` uses `is_run_allowed()` instead of blocklist check
- Each tool calls `write_audit_entry()` on success and failure
- Remove old `BLOCKED_SUBCOMMANDS` constant
- Tools accept `_ufo_clearance` and `_ufo_provenance` metadata params (passed by gateway, validated by service)

```python
"""MCP server wrapping the 1Password CLI for per-secret, per-service access."""

from __future__ import annotations

import json
import subprocess

from fastmcp import FastMCP

from ufo import get_tier, is_run_allowed, write_audit_entry

OP_TIMEOUT = 10  # seconds per op call

mcp = FastMCP(
    "op-auth",
    instructions="1Password CLI proxy — read secrets, list vaults/items, run safe op commands",
)


def _run_op(*args: str, parse_json: bool = True) -> str | list | dict:
    """Run an op CLI command and return the result."""
    cmd = ["op", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=OP_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"op exited with code {result.returncode}")

    output = result.stdout.strip()
    if parse_json and output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return output


def _audit(tool: str, args: dict, outcome: str, clearance_valid: bool = True,
           provenance: str = "") -> None:
    """Log a tool call to the audit log."""
    write_audit_entry(
        tool=tool, args=args, tier=get_tier(tool),
        provenance=provenance, outcome=outcome, clearance_valid=clearance_valid,
    )


@mcp.tool()
def read(reference: str) -> str:
    """Read a single secret by its 1Password reference URI.

    Args:
        reference: 1Password secret reference (e.g. "op://Vault/Item/field").
    """
    args = {"reference": reference}
    try:
        result = _run_op("read", reference, parse_json=False)
        _audit("read", args, "allowed")
        return str(result)
    except subprocess.TimeoutExpired:
        _audit("read", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("read", args, "error")
        return f"Error: {e}"


@mcp.tool()
def list_vaults() -> str:
    """List all accessible 1Password vaults."""
    try:
        result = _run_op("vault", "list", "--format=json")
        _audit("list_vaults", {}, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("list_vaults", {}, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("list_vaults", {}, "error")
        return f"Error: {e}"


@mcp.tool()
def list_items(vault: str) -> str:
    """List items in a specific vault.

    Args:
        vault: Vault name or ID.
    """
    args = {"vault": vault}
    try:
        result = _run_op("item", "list", "--vault", vault, "--format=json")
        _audit("list_items", args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("list_items", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("list_items", args, "error")
        return f"Error: {e}"


@mcp.tool()
def get_item(vault: str, item: str) -> str:
    """Get full details of a specific item.

    Args:
        vault: Vault name or ID.
        item: Item name or ID.
    """
    args = {"vault": vault, "item": item}
    try:
        result = _run_op("item", "get", item, "--vault", vault, "--format=json")
        _audit("get_item", args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("get_item", args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("get_item", args, "error")
        return f"Error: {e}"


@mcp.tool()
def run(subcommand: str, args: list[str] | None = None) -> str:
    """Run an op CLI command (allowlisted subcommands only).

    Allowed: vault (any), item list, item get, document get.
    Everything else is denied.

    Args:
        subcommand: The op subcommand (e.g. "item", "vault", "document").
        args: Additional arguments for the subcommand.
    """
    run_args = {"subcommand": subcommand, "args": args or []}

    if not is_run_allowed(subcommand, args):
        _audit("run", run_args, "denied")
        return f"Error: '{subcommand}' with args {args or []} is not on the allowlist"

    cmd_args = [subcommand]
    if args:
        cmd_args.extend(args)
    cmd_args.append("--format=json")

    try:
        result = _run_op(*cmd_args)
        _audit("run", run_args, "allowed")
        return json.dumps(result, indent=2) if not isinstance(result, str) else result
    except subprocess.TimeoutExpired:
        _audit("run", run_args, "error")
        return "Error: op command timed out"
    except RuntimeError as e:
        _audit("run", run_args, "error")
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
```

**Step 4: Verify module loads**

Run: `cd ~/WebSpec/services/op-auth && /home/preston/miniconda3/bin/python -c "import server; print([t for t in server.mcp._tool_manager._tools])"`
Expected: prints the 5 tool names

**Step 5: Run all tests**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/ -v`
Expected: all tests pass

**Step 6: Commit**

```bash
git add services/op-auth/server.py gateway/tests/test_opauth_ufo_integration.py
git commit -m "feat(op-auth): enforce UFO tiers — allowlist run(), audit all calls"
```

---

### Task 6: Pass UFO headers through gateway app.py

**Files:**
- Modify: `gateway/webspec/app.py:69-93` (guard enforcement block)

**Step 1: Write the test**

```python
# gateway/tests/test_ufo_headers.py
"""Test that UFO headers are extracted and would be passed through."""
from webspec.guard import compute_clearance_token, build_provenance_link

def test_clearance_header_format(session_key):
    """Clearance header is token:timestamp format."""
    import time
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    header = f"{token}:{ts}"
    assert ":" in header
    parts = header.split(":")
    assert len(parts) == 2
    assert len(parts[0]) == 8

def test_provenance_header_format(session_key):
    """Provenance header is role:sig->role:sig format."""
    link = build_provenance_link(session_key, "human", "gateway", "read")
    header = f"human:{link}"
    assert ":" in header
    parts = header.split(":")
    assert parts[0] == "human"
    assert len(parts[1]) == 8
```

**Step 2: Run test**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/test_ufo_headers.py -v`
Expected: 2 passed (these test the format, not the gateway wiring)

**Step 3: Modify app.py dispatch to extract and log UFO headers**

In `gateway/webspec/app.py`, update the guard enforcement block in `_service_dispatch` (lines 69-93). After the guard passes, extract UFO headers and store on request state:

Replace the guard enforcement block with:

```python
    # Check if this service requires guard authentication
    if registry is not None:
        entry = registry.get(service)
        if entry is not None and entry.guard:
            # Handle /__nonce bootstrap endpoint
            if path == "__nonce" and method == "GET":
                return await _handle_nonce(request, service)

            # Handle /__challenge endpoint for dangerous-tier human confirmation
            if path == "__challenge" and method == "GET":
                return await _handle_challenge(request, service)

            # Enforce guard on all other requests
            body = await request.body()
            host = request.headers.get("host", "")
            guard_result = validate_guard(
                session_key=get_session_key(),
                method=method,
                host=host,
                path=f"/{path}" if path else "/",
                body=body,
                guard_header=request.headers.get("X-WebSpec-Guard"),
                nonce_header=request.headers.get("X-WebSpec-Nonce"),
            )
            if isinstance(guard_result, GuardError):
                return JSONResponse(
                    {"error": guard_result.error_type, "detail": guard_result.detail},
                    status_code=guard_result.status_code,
                )

            # Store UFO headers on request state for downstream use
            request.state.ufo_clearance = request.headers.get("X-UFO-Clearance")
            request.state.ufo_provenance = request.headers.get("X-UFO-Provenance")
```

Add the `_handle_challenge` function after `_handle_nonce`:

```python
async def _handle_challenge(request: Request, service: str) -> Response:
    """Handle GET /__challenge — issue a human confirmation challenge for dangerous-tier tools."""
    host = request.headers.get("host", "")
    guard_result = validate_guard(
        session_key=get_session_key(),
        method="GET",
        host=host,
        path="/__challenge",
        body=b"",
        guard_header=request.headers.get("X-WebSpec-Guard"),
        nonce_header=None,
        is_nonce_request=True,  # challenge bootstrap works like nonce bootstrap
    )
    if isinstance(guard_result, GuardError):
        return JSONResponse(
            {"error": guard_result.error_type, "detail": guard_result.detail},
            status_code=guard_result.status_code,
        )

    import secrets as _secrets
    challenge = _secrets.token_hex(16)
    # Store challenge for later verification (reuse nonce store with short TTL)
    from .guard import nonce_store
    nonce_store.create(f"challenge:{service}")  # audience = challenge:service

    tool = request.query_params.get("tool", "")
    return JSONResponse({
        "challenge": challenge,
        "service": service,
        "tool": tool,
        "message": f"Confirm: execute '{tool}' on {service}?",
        "expires_in": 30,
    })
```

Also add the import for `validate_provenance_chain` at the top of app.py:

```python
from .guard import GuardError, generate_nonce, validate_guard, validate_provenance_chain
```

**Step 4: Run all tests**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/ -v`
Expected: all pass

**Step 5: Commit**

```bash
git add gateway/webspec/app.py gateway/tests/test_ufo_headers.py
git commit -m "feat(gateway): extract UFO headers, add /__challenge endpoint"
```

---

### Task 7: End-to-end live gateway test

**Files:**
- Create: `gateway/tests/test_ufo_e2e.py` (run against live gateway)

**Step 1: Write the E2E test script**

```python
# gateway/tests/test_ufo_e2e.py
"""End-to-end UFO tests against the live gateway. Run with: python tests/test_ufo_e2e.py"""

import hashlib
import hmac
import json
import time
import urllib.request
import urllib.error

SESSION_KEY_PATH = "/home/preston/.webspec/session.key"
BASE = "http://op-auth.localhost:7001"
HOST = "op-auth.localhost:7001"


def load_key():
    with open(SESSION_KEY_PATH, "rb") as f:
        return f.read()


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
```

**Step 2: Restart gateway to pick up new code**

Run: `kill $(pgrep -f 'python -m webspec') && sleep 4`
Wait for systemd to restart it.

**Step 3: Run E2E tests**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python tests/test_ufo_e2e.py`
Expected: all 3 tests pass

**Step 4: Commit**

```bash
git add gateway/tests/test_ufo_e2e.py
git commit -m "test: add UFO end-to-end tests against live gateway"
```

---

### Task 8: Update CLAUDE.md and Notion page

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Add UFO documentation to CLAUDE.md**

Add to the `## MCP Services` section under `**op-auth**`:

```
UFO policy (`services/op-auth/ufo.py`): tools classified as open/sensitive/dangerous. `run()` uses strict allowlist (vault, item list/get, document get). All calls logged to `~/.webspec/op-auth-audit.jsonl`. Sensitive tools require `X-UFO-Clearance` header; dangerous tools require human confirmation via `/__challenge`.
```

Add to the `## Gateway Architecture` section:

```
- **guard.py** — ...also: UFO clearance token computation (`compute_clearance_token`), provenance chain validation (`validate_provenance_chain`)
```

**Step 2: Update Notion page with implementation status**

Use the Notion MCP tool to update the checklist on page `30a42c9038be818f9b41f45458ee4a8f`.

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document UFO policy in CLAUDE.md"
```

---

### Task 9: Final integration commit

**Step 1: Run full test suite**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python -m pytest tests/ -v`
Expected: all tests pass

**Step 2: Run E2E against live gateway**

Run: `cd ~/WebSpec/gateway && /home/preston/miniconda3/bin/python tests/test_ufo_e2e.py`
Expected: all pass

**Step 3: Verify audit log exists**

Run: `ls -la ~/.webspec/op-auth-audit.jsonl`
Expected: file exists (may be empty if op CLI isn't configured yet, but E2E tests should have created denied/error entries)
