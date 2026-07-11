# WebSpec Track A — Ship & Harden — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the live personal gateway into a self-hostable (tier B), hardened, `docker compose`–deployable WebSpec whose auth root is a password manager and whose docs match the code.

**Architecture:** Modify the existing `webspec` gateway package in place. Replace the local `session.key` with a vault-sourced `WEBSPEC_GUARD_KEY`. Enforce a "public exposure requires guard" invariant at both the gateway and the Caddy-config generator. Externalize host-path config. Add a `docker/` compose stack. Reconcile `docs/`.

**Tech Stack:** Python 3.11, Starlette, uvicorn, pytest, Caddy, Docker Compose, 1Password `op` CLI.

## Global Constraints

- Python `>=3.11`; keep deps within existing floors (`starlette>=0.35`, `uvicorn>=0.30`, `fastmcp>=2.14,<3`, `mcp>=1.25`). Do not add heavy new runtime deps in this track.
- Tests: `pytest`, run from `gateway/`. Match existing style in `gateway/tests/` (function/class tests, `tmp_path`, `monkeypatch`, injected `path=`/env).
- Never silently generate a persistent auth secret. Missing guard key ⇒ fail closed.
- Do not break the live symlinks/service names in `CLAUDE.md` (`~/MCP/webspec-gateway`, `webspec-gateway.service`).
- Every guard key value is derived identically on gateway and client: 64-hex ⇒ `bytes.fromhex`; otherwise ⇒ `sha256(utf8(value))`.
- Commit after every task with a `feat:`/`fix:`/`docs:`/`test:` message.

---

### Task 1: Vault-sourced guard key (delete `session.key`)

**Files:**
- Modify: `gateway/webspec/config.py` (replace `get_session_key`, remove `WEBSPEC_DIR`/`SESSION_KEY_PATH` usage for the key)
- Test: `gateway/tests/test_guard_key.py` (create)

**Interfaces:**
- Consumes: `os.environ["WEBSPEC_GUARD_KEY"]`, optional `WEBSPEC_GUARD_KEY_DEV_EPHEMERAL`.
- Produces: `get_session_key() -> bytes` (32 bytes); `GuardKeyError(RuntimeError)`; `_derive_guard_key(raw: str) -> bytes`. Callers in `app.py`/`handlers.py` are unchanged (same signature).

- [ ] **Step 1: Write the failing tests**

```python
# gateway/tests/test_guard_key.py
"""Guard key is sourced from the environment (populated from a password manager), never a file."""
import hashlib
import pytest
import webspec.config as config
from webspec.config import GuardKeyError, get_session_key


@pytest.fixture(autouse=True)
def _reset_ephemeral(monkeypatch):
    # Ensure a clean env + reset the cached dev key between tests.
    monkeypatch.delenv("WEBSPEC_GUARD_KEY", raising=False)
    monkeypatch.delenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", raising=False)
    config._dev_ephemeral_key = None
    yield
    config._dev_ephemeral_key = None


def test_hex_key_decoded(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "01" * 32)  # 64 hex chars
    assert get_session_key() == b"\x01" * 32


def test_passphrase_key_derived(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "hunter2")
    assert get_session_key() == hashlib.sha256(b"hunter2").digest()
    assert len(get_session_key()) == 32


def test_missing_key_fails_closed():
    with pytest.raises(GuardKeyError):
        get_session_key()


def test_dev_ephemeral_is_stable(monkeypatch):
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    k1 = get_session_key()
    k2 = get_session_key()
    assert k1 == k2 and len(k1) == 32


def test_no_session_key_file_created(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WEBSPEC_GUARD_KEY", "02" * 32)
    get_session_key()
    assert not (tmp_path / ".webspec" / "session.key").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gateway && python -m pytest tests/test_guard_key.py -v`
Expected: FAIL (`ImportError: cannot import name 'GuardKeyError'`).

- [ ] **Step 3: Rewrite `get_session_key` in `config.py`**

Add `import hashlib` and `import sys` at the top. Replace the `WEBSPEC_DIR`/`SESSION_KEY_PATH`/`get_session_key` block (currently lines ~112-128) with:

```python
WEBSPEC_DIR = Path.home() / ".webspec"  # still used for audit logs elsewhere

_dev_ephemeral_key: bytes | None = None


class GuardKeyError(RuntimeError):
    """Raised when no guard key is available and none may be safely generated."""


def _derive_guard_key(raw: str) -> bytes:
    """64-hex → raw 32 bytes; anything else → sha256(utf8) so any passphrase works."""
    raw = raw.strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return hashlib.sha256(raw.encode()).digest()


def get_session_key() -> bytes:
    """Return the 32-byte guard key, sourced from the password manager via env.

    Populate WEBSPEC_GUARD_KEY from your vault at launch, e.g.:
        export WEBSPEC_GUARD_KEY=$(op read "op://WebSpec/gateway-guard/key")
    Fails closed if absent (no silent random key). Dev-only escape hatch:
    WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 mints an insecure in-memory key.
    """
    raw = os.environ.get("WEBSPEC_GUARD_KEY")
    if raw:
        return _derive_guard_key(raw)

    if os.environ.get("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL") == "1":
        global _dev_ephemeral_key
        if _dev_ephemeral_key is None:
            _dev_ephemeral_key = secrets.token_bytes(32)
            print(
                "WARNING: WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 — using an insecure "
                "in-memory guard key (dev only).",
                file=sys.stderr,
            )
        return _dev_ephemeral_key

    raise GuardKeyError(
        "WEBSPEC_GUARD_KEY is not set. Source it from your password manager, e.g. "
        "`export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')`. "
        "For local dev only, set WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd gateway && python -m pytest tests/test_guard_key.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/config.py gateway/tests/test_guard_key.py
git commit -m "feat(gateway): source guard key from password manager env, fail closed"
```

---

### Task 2: Enforce "public exposure requires guard" at the gateway

**Files:**
- Modify: `gateway/webspec/app.py` (`_service_dispatch`, add `_is_public_host`)
- Test: `gateway/tests/test_public_guard_invariant.py` (create)

**Interfaces:**
- Consumes: `os.environ.get("WEBSPEC_DOMAIN")`, `registry.get(service).guard`, request `Host` header.
- Produces: gateway returns HTTP 403 `unguarded_public` for any request whose host is the public domain and whose service is not guarded — before dispatch.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_public_guard_invariant.py
"""An unguarded service must never be reachable on the public domain."""
import pytest
from starlette.testclient import TestClient

import webspec.app as appmod
from webspec.config import ServiceEntry


class _FakeRegistry:
    def __init__(self, entry): self._entry = entry
    def get(self, name): return self._entry if name == "mail-proton" else None
    def names(self): return ["mail-proton"]
    @property
    def services(self): return {"mail-proton": self._entry}


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setenv("WEBSPEC_DOMAIN", "i-a-m.live")
    monkeypatch.setenv("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL", "1")
    entry = ServiceEntry(name="mail-proton", original_name="mail-proton",
                         transport_type="stdio", command="x", guard=False)
    appmod.registry = _FakeRegistry(entry)
    app = appmod.create_app()
    appmod.registry = _FakeRegistry(entry)  # create_app resets it; pin our fake
    return TestClient(app)


def test_unguarded_service_blocked_on_public_domain(public_client):
    r = public_client.get("/", headers={"Host": "mail-proton.i-a-m.live"})
    assert r.status_code == 403
    assert r.json()["error"] == "unguarded_public"


def test_unguarded_service_allowed_on_localhost(public_client):
    # localhost is not the public domain → normal handling (503/200, not 403)
    r = public_client.get("/", headers={"Host": "mail-proton.localhost"})
    assert r.status_code != 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_public_guard_invariant.py -v`
Expected: FAIL (unguarded request returns 200/503, not 403).

- [ ] **Step 3: Implement the invariant in `app.py`**

Add near the top of `app.py` after imports:

```python
def _is_public_host(host: str) -> bool:
    """True if the Host header targets the configured public domain (not localhost)."""
    public_domain = os.environ.get("WEBSPEC_DOMAIN")
    if not public_domain:
        return False
    hostname = host.split(":")[0]
    return hostname == public_domain or hostname.endswith("." + public_domain)
```

In `_service_dispatch`, immediately after `entry = registry.get(service)` resolution (inside the
`if registry is not None:` block, before the guard checks), add:

```python
        entry = registry.get(service)
        host_header = request.headers.get("host", "")
        if entry is not None and not entry.guard and _is_public_host(host_header):
            return JSONResponse(
                {"error": "unguarded_public",
                 "detail": "Unguarded services are not exposed on the public domain."},
                status_code=403,
            )
        if entry is not None and entry.guard:
            ...  # existing guard block unchanged
```

(Reuse the already-fetched `entry`; do not call `registry.get` twice.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_public_guard_invariant.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/app.py gateway/tests/test_public_guard_invariant.py
git commit -m "fix(gateway): block unguarded services on the public domain"
```

---

### Task 3: Caddy generator emits public host only for guarded services

**Files:**
- Modify: `gateway/webspec/caddy.py` (`generate_site_block`)
- Test: `gateway/tests/test_caddy.py` (extend — file already exists)

**Interfaces:**
- Consumes: `generate_site_block(name, domain, guard=..., ...)`.
- Produces: when `guard=False`, the emitted block's host line contains only `{name}.localhost:{port}` (no `{name}.{domain}` host); when `guard=True`, both hosts appear. This is defense-in-depth paired with Task 2.

- [ ] **Step 1: Write the failing test** (append to `gateway/tests/test_caddy.py`)

```python
def test_unguarded_block_has_no_public_host():
    from webspec.caddy import generate_site_block
    block = generate_site_block(name="mail-proton", domain="i-a-m.live", guard=False)
    assert "mail-proton.localhost" in block
    assert "mail-proton.i-a-m.live" not in block


def test_guarded_block_has_public_host():
    from webspec.caddy import generate_site_block
    block = generate_site_block(name="op-auth", domain="i-a-m.live", guard=True)
    assert "op-auth.localhost" in block
    assert "op-auth.i-a-m.live" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_caddy.py -k public_host -v`
Expected: FAIL (`mail-proton.i-a-m.live` currently present in every block).

- [ ] **Step 3: Implement in `caddy.py`**

In `generate_site_block`, replace the `hosts = ...` line with a guard-conditional host list:

```python
    if guard:
        hosts = f"http://{name}.localhost:{caddy_port}, http://{name}.{domain}:{caddy_port}"
    else:
        # Unguarded services are localhost-only (see public-guard invariant).
        hosts = f"http://{name}.localhost:{caddy_port}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_caddy.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/caddy.py gateway/tests/test_caddy.py
git commit -m "fix(caddy): only publish public host block for guarded services"
```

---

### Task 4: Bind gateway to localhost by default

**Files:**
- Modify: `gateway/webspec/__main__.py`
- Test: `gateway/tests/test_bind_default.py` (create)

**Interfaces:**
- Consumes: `os.environ.get("WEBSPEC_HOST")`.
- Produces: `resolve_bind_host() -> str` returning `"127.0.0.1"` unless `WEBSPEC_HOST` is set.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_bind_default.py
import webspec.__main__ as m


def test_default_bind_is_localhost(monkeypatch):
    monkeypatch.delenv("WEBSPEC_HOST", raising=False)
    assert m.resolve_bind_host() == "127.0.0.1"


def test_bind_override(monkeypatch):
    monkeypatch.setenv("WEBSPEC_HOST", "0.0.0.0")
    assert m.resolve_bind_host() == "0.0.0.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_bind_default.py -v`
Expected: FAIL (`resolve_bind_host` not defined).

- [ ] **Step 3: Implement in `__main__.py`**

Add the helper and use it in `main()`:

```python
def resolve_bind_host() -> str:
    """Default to loopback; require an explicit override to bind all interfaces."""
    return os.environ.get("WEBSPEC_HOST", "127.0.0.1")
```

In `uvicorn.run(...)`, change `host=os.environ.get("WEBSPEC_HOST", "0.0.0.0")` to `host=resolve_bind_host()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_bind_default.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/__main__.py gateway/tests/test_bind_default.py
git commit -m "fix(gateway): bind loopback by default, require explicit WEBSPEC_HOST override"
```

---

### Task 5: Lock CORS to an explicit allowlist

**Files:**
- Modify: `gateway/webspec/app.py` (`create_app` middleware)
- Test: `gateway/tests/test_cors.py` (create)

**Interfaces:**
- Consumes: `os.environ.get("WEBSPEC_CORS_ORIGINS")` (comma-separated).
- Produces: `cors_origins() -> list[str]` — `[]` by default, parsed list when set. Middleware uses it instead of `["*"]`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_cors.py
import webspec.app as appmod


def test_cors_empty_by_default(monkeypatch):
    monkeypatch.delenv("WEBSPEC_CORS_ORIGINS", raising=False)
    assert appmod.cors_origins() == []


def test_cors_parses_list(monkeypatch):
    monkeypatch.setenv("WEBSPEC_CORS_ORIGINS", "https://a.example, https://b.example")
    assert appmod.cors_origins() == ["https://a.example", "https://b.example"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_cors.py -v`
Expected: FAIL (`cors_origins` not defined).

- [ ] **Step 3: Implement in `app.py`**

Add:

```python
def cors_origins() -> list[str]:
    """Explicit CORS allowlist from WEBSPEC_CORS_ORIGINS (comma-separated). Empty by default."""
    raw = os.environ.get("WEBSPEC_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]
```

In `create_app`, change the `CORSMiddleware` config from `allow_origins=["*"]` (and `allow_methods=["*"]`, `allow_headers=["*"]`) to:

```python
                CORSMiddleware,
                allow_origins=cors_origins(),
                allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"],
                allow_headers=["X-WebSpec-Guard", "X-WebSpec-Nonce", "X-Gimme-Definer",
                               "X-UFO-Clearance", "X-UFO-Provenance", "Content-Type"],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_cors.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/app.py gateway/tests/test_cors.py
git commit -m "fix(gateway): replace wildcard CORS with explicit allowlist"
```

---

### Task 6: Externalize config paths (`WEBSPEC_CONFIG`, `WEBSPEC_ENV_FILE`)

**Files:**
- Modify: `gateway/webspec/config.py` (`ServiceRegistry.__init__` default), `gateway/webspec/config_writer.py` (module path constants → functions)
- Test: `gateway/tests/test_config_paths.py` (create)

**Interfaces:**
- Consumes: `WEBSPEC_CONFIG`, `WEBSPEC_ENV_FILE`.
- Produces: `default_config_path() -> Path` and `default_env_path() -> Path` in `config_writer.py`; `ServiceRegistry()` with no args reads `WEBSPEC_CONFIG` then falls back to `~/.claude.json`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_config_paths.py
import json
import webspec.config_writer as cw
from webspec.config import ServiceRegistry


def test_default_config_path_env(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    assert cw.default_config_path() == p


def test_registry_reads_env_config(monkeypatch, tmp_path):
    p = tmp_path / "alt.json"
    p.write_text(json.dumps({"mcpServers": {"svc-x": {"type": "http", "url": "http://x"}}}))
    monkeypatch.setenv("WEBSPEC_CONFIG", str(p))
    reg = ServiceRegistry()
    assert "svc-x" in reg.names()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_config_paths.py -v`
Expected: FAIL (`default_config_path` not defined; registry ignores env).

- [ ] **Step 3: Implement**

In `config_writer.py`, replace the module constants with functions:

```python
def default_config_path() -> Path:
    return Path(os.environ.get("WEBSPEC_CONFIG", str(Path.home() / ".claude.json")))


def default_env_path() -> Path:
    return Path(os.environ.get("WEBSPEC_ENV_FILE", str(Path.home() / ".env")))
```

Update `_read_claude_config`, `_write_claude_config`, `add_service`, `remove_service`,
`list_services` to use `path or default_config_path()`, and `add_env_var`/`remove_env_var` to use
`path or default_env_path()` (replace references to `_CLAUDE_CONFIG` / `_ENV_FILE`). Add `import os`.

In `config.py`, change `ServiceRegistry.__init__`:

```python
    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path(os.environ.get("WEBSPEC_CONFIG", str(Path.home() / ".claude.json")))
        self._config_path = config_path
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_config_paths.py tests/test_config_writer.py -v`
Expected: PASS (new + existing config_writer tests still green).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec/config.py gateway/webspec/config_writer.py gateway/tests/test_config_paths.py
git commit -m "feat(gateway): externalize config/env paths via WEBSPEC_CONFIG/WEBSPEC_ENV_FILE"
```

---

### Task 7: Docker Compose stack + guard-key entrypoint

**Files:**
- Create: `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/entrypoint.sh`, `docker/README.md`
- Test: `gateway/tests/test_compose_config.py` (create — validates compose is well-formed and the entrypoint sources the key)

**Interfaces:**
- Consumes: `WEBSPEC_GUARD_KEY` (or `OP_GUARD_KEY_REF` + `op`), `WEBSPEC_CONFIG` mount.
- Produces: a `docker compose` stack (`caddy`, `gateway`, `registry`) that boots the gateway with a vault-sourced key.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_compose_config.py
"""The compose stack is well-formed and the entrypoint sources the guard key from the vault."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repo root


def test_entrypoint_sources_guard_key():
    text = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert "OP_GUARD_KEY_REF" in text
    assert "op read" in text
    assert "WEBSPEC_GUARD_KEY" in text


def test_compose_declares_three_services():
    text = (ROOT / "docker" / "docker-compose.yml").read_text()
    for svc in ("caddy:", "gateway:", "registry:"):
        assert svc in text


@pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")
def test_compose_config_valid():
    r = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker" / "docker-compose.yml"), "config"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_compose_config.py -v`
Expected: FAIL (files do not exist).

- [ ] **Step 3: Create the Docker files**

`docker/entrypoint.sh`:

```bash
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
```

`docker/Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY gateway/ /app/gateway/
RUN pip install --no-cache-dir ./gateway
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "webspec"]
```

`docker/docker-compose.yml`:

```yaml
services:
  caddy:
    image: caddy:2
    ports:
      - "7001:7001"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy/conf.d:/etc/caddy/conf.d:ro
    depends_on: [gateway]

  gateway:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    environment:
      WEBSPEC_INTERNAL_PORT: "7002"
      WEBSPEC_HOST: "0.0.0.0"          # inside the compose network, fronted by caddy
      WEBSPEC_CONFIG: "/config/claude.json"
      WEBSPEC_GUARD_KEY: "${WEBSPEC_GUARD_KEY:-}"
      OP_GUARD_KEY_REF: "${OP_GUARD_KEY_REF:-}"
    volumes:
      - ${WEBSPEC_CONFIG_HOST:-./claude.json}:/config/claude.json:ro
    expose:
      - "7002"

  registry:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    command: ["python", "-m", "webspec_registry"]
    environment:
      WEBSPEC_INTERNAL_PORT: "7003"
      WEBSPEC_GATEWAY_URL: "http://gateway:7002"
    expose:
      - "7003"
```

`docker/README.md`: document `WEBSPEC_GUARD_KEY`/`OP_GUARD_KEY_REF`, the `WEBSPEC_CONFIG_HOST` mount,
and the "bring your own MCP service" pattern (http services join the stack; stdio services like
op-auth/mail-proton run on the host and are referenced by the mounted config). Note the `registry`
service depends on Plan 2 (`webspec_registry`); until that lands, comment out the `registry` block
or keep it — `docker compose config` still validates.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_compose_config.py -v`
Expected: PASS (`test_compose_config_valid` may skip if docker absent).

- [ ] **Step 5: Commit**

```bash
git add docker/ gateway/tests/test_compose_config.py
git commit -m "feat: add docker compose stack with vault-sourced guard-key entrypoint"
```

---

### Task 8: Docs reconciliation

**Files:**
- Modify: `docs/index.md` (add status matrix), `docs/url-grammar/core-syntax.md`,
  `docs/url-grammar/complete-grammar-ebnf.md`, `docs/url-grammar/object-type-system.md`
  (collapse to one normative grammar page + examples), `docs/discovery/*.md` (remove the banned
  `object.provider` form, e.g. `POST /message.slack` → `POST /messages` with a note that the
  path is the MCP tool name in the current implementation)
- Create: `docs/ROADMAP-C.md`
- Test: `docs/tests/test_docs_consistency.py` (create)

**Interfaces:**
- Produces: docs whose normative sections match the code; a machine-checkable consistency guard.

- [ ] **Step 1: Write the failing test**

```python
# docs/tests/test_docs_consistency.py
"""Guard against the spec drifting from the implementation again."""
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1]


def test_status_matrix_present():
    idx = (DOCS / "index.md").read_text()
    assert "Implemented" in idx and "Proposed" in idx


def test_discovery_does_not_use_banned_provider_suffix():
    # url-grammar bans `object.provider`; discovery must not demonstrate it.
    for md in (DOCS / "discovery").glob("*.md"):
        text = md.read_text()
        for banned in ("/message.slack", "/file.gdrive", "/spreadsheet.gsheets"):
            assert banned not in text, f"{md.name} still uses banned form {banned}"


def test_roadmap_c_exists():
    assert (DOCS / "ROADMAP-C.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd docs && python -m pytest tests/test_docs_consistency.py -v`
Expected: FAIL (no status matrix; discovery still uses `/message.slack`; no ROADMAP-C).

- [ ] **Step 3: Make the edits**

1. In `docs/index.md`, replace the `## Status` table with an Implemented-vs-Proposed matrix, e.g.:

```markdown
## Status

| Section | State |
|---|---|
| Subdomain routing (path = MCP tool name) | **Implemented (B)** |
| HMAC guard + nonce, password-manager–sourced key | **Implemented (B)** |
| Definer verbs | **Implemented (B)** |
| op-auth UFO tiers | **Implemented (B)** |
| Semantic resolution / embedding registry | Proposed (see registry package) |
| REST collection hierarchy, format suffixes, DELETE | **Proposed (C)** |
| OAuth/JWT, multi-tenant, keychain FaceID flow | **Proposed (C)** |
```

2. In the `discovery/*.md` files, rewrite every `POST /message.slack`-style example to the
   implementation's form (the path is the MCP tool name), adding one sentence: "In the current
   implementation the path segment is the MCP tool name (e.g. `send_message`); the `/collection/id`
   REST form is Proposed (C)." Remove the banned strings the test checks for.

3. Collapse the three url-grammar pages: keep `complete-grammar-ebnf.md` as the single normative
   grammar; reduce `core-syntax.md` to a short human overview that links to it; fold
   `object-type-system.md`'s unique content (the collections tables) into an "Examples" section and
   delete the duplicated "subdomain IS provider" prose. Ensure the "REST hierarchy" grammar is
   labeled Proposed (C) since the code routes by tool name.

4. Create `docs/ROADMAP-C.md` indexing every `TODO(C)` from the spec §8 with a one-line pointer to
   its design section.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd docs && python -m pytest tests/test_docs_consistency.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add docs/index.md docs/url-grammar/ docs/discovery/ docs/ROADMAP-C.md docs/tests/test_docs_consistency.py
git commit -m "docs: reconcile spec with implementation, add status matrix and ROADMAP-C"
```

---

## Self-Review

- **Spec coverage:** §3.1 auth → Task 1; §3.2 invariants → Tasks 2,3,4,5; §3.3 externalization → Task 6; §3.4 topology → Task 7; §6 docs → Task 8. Guard-by-default (§3.2 #4) is realized by Tasks 2+3 (unguarded services can't reach the public domain) plus the existing `ctl.py` `--guard` default; no separate task needed.
- **Deferred to Plan 2:** the `registry` service image `command: python -m webspec_registry` (Task 7) depends on Plan 2; documented inline.
- **Type consistency:** `get_session_key() -> bytes` signature unchanged (callers untouched); `_is_public_host`, `cors_origins`, `resolve_bind_host`, `default_config_path`, `default_env_path` are new and referenced only where defined.
- **`wiki/` de-dup** (§6.5): tracked as a follow-up in `docs/ROADMAP-C.md` (mechanical mirror change, not code); note it there during Task 8 rather than hand-editing both trees now.
