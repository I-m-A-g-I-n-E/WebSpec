# WebSpec Registry — Catalog, Resolver & Keychain Graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new `webspec_registry` package that harvests the gateway's tool catalog, resolves natural-language intent to ranked tools (goal 2), and emits a services→nouns→verbs graph plus a metadata-only 1Password secrets catalog (goal 3).

**Architecture:** A standalone package (sibling to `webspec`) with a shared Trunk (`records`, `normalize`, `catalog`) feeding two independent branches: a resolver (`matcher`, `resolver`) and a keychain graph (`keychain`, `graph`). A small Starlette service (`app`, `__main__`) exposes `/resolve`, `/catalog`, `/graph`. The service is **localhost-only** for tier B (public exposure requires porting the guard middleware — a `TODO(C)`).

**Tech Stack:** Python 3.11, Starlette, uvicorn, pytest. Default matcher is pure-Python lexical (zero external deps); embeddings are an opt-in `TODO(C)`.

## Global Constraints

- Python `>=3.11`. **No heavy new runtime deps** — the default resolver must work with the stdlib only.
- Package lives at `gateway/webspec_registry/`; tests at `gateway/tests/test_registry_*.py`; run from `gateway/`.
- **Keychain hard invariant:** never call the op-auth `read()` tool and never surface a secret field *value*. Only titles, categories, URLs, and timestamps may leave 1Password. This is test-enforced.
- The Trunk (Tasks 1-3) must land before the branches (Tasks 4-5 resolver; Tasks 6-7 graph). Tasks 4-5 and 6-7 are independent of each other and may be done in parallel.
- Every C extension point is a `# TODO(C): …` comment plus a line in `docs/ROADMAP-C.md`.
- Commit after every task.

---

### Task 1: Package scaffold + record types

**Files:**
- Create: `gateway/webspec_registry/__init__.py`, `gateway/webspec_registry/records.py`
- Modify: `gateway/pyproject.toml` (register the second package + a console script)
- Test: `gateway/tests/test_registry_records.py` (create)

**Interfaces:**
- Produces: `ToolRecord(service, tool, description, verb, noun, tier, input_schema)` and
  `AccountRecord(vault, item, title, category, urls, fields_present, updated_at, linked_service)`,
  both frozen dataclasses. Every later task depends on these exact field names/order.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_records.py
from webspec_registry.records import ToolRecord, AccountRecord


def test_tool_record_fields():
    r = ToolRecord(service="mail-proton", tool="send_email", description="Send an email",
                   verb="send", noun="message", tier="sensitive", input_schema={"type": "object"})
    assert r.service == "mail-proton" and r.verb == "send" and r.noun == "message"
    assert r.tier == "sensitive" and r.input_schema["type"] == "object"


def test_account_record_defaults():
    a = AccountRecord(vault="Personal", item="OpenAI", title="OpenAI", category="API_CREDENTIAL")
    assert a.urls == () and a.fields_present == () and a.linked_service is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_records.py -v`
Expected: FAIL (`ModuleNotFoundError: webspec_registry`).

- [ ] **Step 3: Create the package and records**

`gateway/webspec_registry/__init__.py`:

```python
"""WebSpec registry: tool catalog, semantic resolver, and keychain graph."""
```

`gateway/webspec_registry/records.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolRecord:
    service: str
    tool: str
    description: str
    verb: str
    noun: str
    tier: str  # "open" | "sensitive" | "dangerous"
    input_schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountRecord:
    vault: str
    item: str
    title: str
    category: str
    urls: tuple[str, ...] = ()
    fields_present: tuple[str, ...] = ()
    updated_at: str | None = None
    linked_service: str | None = None
```

In `gateway/pyproject.toml`, add the console script under `[project.scripts]`:

```toml
webspec-registry = "webspec_registry.__main__:main"
```

and append a build target so both packages are included:

```toml
[tool.hatch.build.targets.wheel]
packages = ["webspec", "webspec_registry"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_records.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/ gateway/pyproject.toml gateway/tests/test_registry_records.py
git commit -m "feat(registry): scaffold webspec_registry package and record types"
```

---

### Task 2: Normalization (verb/noun canonicalization + tier)

**Files:**
- Create: `gateway/webspec_registry/normalize.py`
- Test: `gateway/tests/test_registry_normalize.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize(tool: str, description: str = "") -> tuple[str, str]` (verb, noun) and
  `tier_of(service: str, tool: str, verb: str) -> str`. Used by `catalog.py` and `matcher.py`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_normalize.py
from webspec_registry.normalize import normalize, tier_of


def test_send_email():
    assert normalize("send_email") == ("send", "message")


def test_list_vaults_singularizes_noun():
    assert normalize("list_vaults") == ("read", "vault")


def test_colloquial_query_verb_and_noun():
    # "fire off a note" → send / message
    assert normalize("fire off a note") == ("send", "message")


def test_tier_opauth_known():
    assert tier_of("op-auth", "run", "invoke") == "dangerous"
    assert tier_of("op-auth", "read", "read") == "sensitive"
    assert tier_of("op-auth", "list_vaults", "read") == "open"


def test_tier_default_by_verb():
    assert tier_of("mail-proton", "send_email", "send") == "sensitive"
    assert tier_of("some-svc", "get_thing", "read") == "open"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_normalize.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `normalize.py`**

```python
from __future__ import annotations

import re

# Canonical verb map (seeded from docs/discovery predicate tables).
VERB_CANON = {
    "send": "send", "post": "send", "notify": "send", "ping": "send", "dm": "send",
    "fire": "send", "shoot": "send", "drop": "send", "email": "send",
    "read": "read", "get": "read", "list": "read", "fetch": "read", "search": "read",
    "grab": "read", "pull": "read", "check": "read", "view": "read",
    "create": "create", "add": "create", "new": "create",
    "delete": "delete", "remove": "delete", "nuke": "delete", "trash": "delete",
    "update": "modify", "modify": "modify", "patch": "modify", "edit": "modify",
    "set": "modify", "tweak": "modify", "fix": "modify", "rename": "modify",
    "run": "invoke", "invoke": "invoke", "execute": "invoke", "exec": "invoke",
}

NOUN_CANON = {
    "email": "message", "message": "message", "note": "message", "dm": "message",
    "msg": "message", "mail": "message", "notification": "message",
    "file": "file", "document": "document", "doc": "document", "page": "document",
    "secret": "secret", "item": "secret", "password": "secret", "credential": "secret",
    "vault": "vault",
}

_READ_VERBS = {"read"}

_OPAUTH_TIERS = {
    "list_vaults": "open", "read": "sensitive", "list_items": "sensitive",
    "get_item": "sensitive", "run": "dangerous",
}


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", s.lower()) if t]


def _canon_noun(tok: str) -> str:
    if tok in NOUN_CANON:
        return NOUN_CANON[tok]
    if tok.endswith("s") and tok[:-1] in NOUN_CANON:  # light singularization (vaults→vault)
        return NOUN_CANON[tok[:-1]]
    return tok


def normalize(tool: str, description: str = "") -> tuple[str, str]:
    """Return (canonical_verb, canonical_noun) for a tool name or free-text query."""
    parts = _tokens(tool)
    verb = ""
    for p in parts:                      # first token that maps to a verb
        if p in VERB_CANON:
            verb = VERB_CANON[p]
            break
    if not verb and parts:
        verb = VERB_CANON.get(parts[0], parts[0])

    noun = ""
    for p in reversed(parts):            # last token that maps to a noun
        c = _canon_noun(p)
        if p in NOUN_CANON or (p.endswith("s") and p[:-1] in NOUN_CANON):
            noun = c
            break
    if not noun and parts:
        noun = _canon_noun(parts[-1])
    return verb, noun


def tier_of(service: str, tool: str, verb: str) -> str:
    """Sensitivity tier. op-auth uses its UFO tiers; others default by verb."""
    if service == "op-auth":
        return _OPAUTH_TIERS.get(tool, "dangerous")
    return "open" if verb in _READ_VERBS else "sensitive"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_normalize.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/normalize.py gateway/tests/test_registry_normalize.py
git commit -m "feat(registry): verb/noun normalization and sensitivity tiers"
```

---

### Task 3: Catalog harvester

**Files:**
- Create: `gateway/webspec_registry/catalog.py`
- Test: `gateway/tests/test_registry_catalog.py` (create)

**Interfaces:**
- Consumes: `ToolRecord`, `normalize`, `tier_of`.
- Produces: `harvest(services: list[str], fetch_tools) -> list[ToolRecord]`, where
  `fetch_tools(service) -> list[dict]` yields dicts with keys `name`, `description`, `inputSchema`.
  Also `http_fetch_tools(gateway_url: str)` factory returning such a callable (used by the service).

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_catalog.py
import pytest
from webspec_registry.catalog import harvest


def _fake_fetch(service):
    data = {
        "mail-proton": [{"name": "send_email", "description": "Send an email", "inputSchema": {}}],
        "op-auth": [{"name": "run", "description": "Run op subcommand", "inputSchema": {}}],
    }
    if service == "broken":
        raise RuntimeError("unreachable")
    return data.get(service, [])


def test_harvest_builds_records():
    recs = harvest(["mail-proton", "op-auth"], _fake_fetch)
    by_tool = {r.tool: r for r in recs}
    assert by_tool["send_email"].verb == "send" and by_tool["send_email"].noun == "message"
    assert by_tool["send_email"].tier == "sensitive"
    assert by_tool["run"].tier == "dangerous"


def test_harvest_skips_unreachable_service():
    recs = harvest(["broken", "mail-proton"], _fake_fetch)
    assert {r.service for r in recs} == {"mail-proton"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_catalog.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `catalog.py`**

```python
from __future__ import annotations

import json
import logging
import urllib.request

from .normalize import normalize, tier_of
from .records import ToolRecord

logger = logging.getLogger("webspec.registry.catalog")


def harvest(services: list[str], fetch_tools) -> list[ToolRecord]:
    """Build ToolRecords for each service. fetch_tools(service) -> list[{name,description,inputSchema}]."""
    records: list[ToolRecord] = []
    for svc in services:
        try:
            tools = fetch_tools(svc)
        except Exception as e:  # a down service must not sink the whole catalog
            logger.warning("catalog: skipping %s (%s)", svc, e)
            continue
        for t in tools:
            name = t.get("name", "")
            desc = t.get("description") or ""
            verb, noun = normalize(name, desc)
            records.append(ToolRecord(
                service=svc, tool=name, description=desc,
                verb=verb, noun=noun, tier=tier_of(svc, name, verb),
                input_schema=t.get("inputSchema") or {},
            ))
    return records


def http_fetch_tools(gateway_url: str):
    """Return a fetch_tools callable that reads OPTIONS {gateway_url}/ per service subdomain.

    The gateway's index (GET {gateway_url}/) lists services; OPTIONS {service}.<host>/ lists tools.
    Here we hit the gateway index for the tool list per service via its JSON `tools` array.
    """
    def _fetch(service: str) -> list[dict]:
        # The gateway serves OPTIONS /{service}/ as {"service","tools":[...]}
        url = gateway_url.rstrip("/") + "/"
        req = urllib.request.Request(url, method="OPTIONS", headers={"Host": f"{service}.localhost"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read())
        return payload.get("tools", [])
    return _fetch
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_catalog.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/catalog.py gateway/tests/test_registry_catalog.py
git commit -m "feat(registry): catalog harvester over gateway OPTIONS"
```

---

### Task 4: Matcher (lexical, pluggable)

**Files:**
- Create: `gateway/webspec_registry/matcher.py`
- Test: `gateway/tests/test_registry_matcher.py` (create)

**Interfaces:**
- Consumes: `ToolRecord`, `normalize`.
- Produces: `Matcher` (ABC with `score(query, record) -> float`) and `LexicalMatcher`. Consumed by `resolver.py`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_matcher.py
from webspec_registry.matcher import LexicalMatcher
from webspec_registry.records import ToolRecord

SEND = ToolRecord("slack", "send_message", "Send a message to a channel", "send", "message", "sensitive")
READ = ToolRecord("gdrive", "get_file", "Download a file", "read", "file", "open")


def test_relevant_scores_higher_than_irrelevant():
    m = LexicalMatcher()
    assert m.score("send a message", SEND) > m.score("send a message", READ)


def test_verb_noun_bonus():
    m = LexicalMatcher()
    # canonical verb+noun match should push score above raw token overlap alone
    assert m.score("fire off a note", SEND) > 0.0


def test_empty_query_is_zero():
    assert LexicalMatcher().score("", SEND) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_matcher.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `matcher.py`**

```python
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .normalize import normalize
from .records import ToolRecord


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[_\W]+", s.lower()) if t}


class Matcher(ABC):
    """Scores how well a natural-language query matches a tool. Range [0.0, 1.0]."""

    @abstractmethod
    def score(self, query: str, record: ToolRecord) -> float: ...


class LexicalMatcher(Matcher):
    """Zero-dependency matcher: token overlap + canonical verb/noun bonus."""

    def score(self, query: str, record: ToolRecord) -> float:
        q = _tokens(query)
        if not q:
            return 0.0
        doc = _tokens(record.tool) | _tokens(record.description) | {record.verb, record.noun}
        overlap = len(q & doc) / len(q)
        qverb, qnoun = normalize(query)
        bonus = 0.0
        if qverb and qverb == record.verb:
            bonus += 0.25
        if qnoun and qnoun == record.noun:
            bonus += 0.25
        return min(1.0, overlap + bonus)

# TODO(C): EmbeddingMatcher — local small model or API backend, multi-vector
# (canonical/predicate/object) search. See docs/discovery/embedding-schema.md.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_matcher.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/matcher.py gateway/tests/test_registry_matcher.py
git commit -m "feat(registry): pluggable lexical matcher (zero-dep default)"
```

---

### Task 5: Resolver (three-way-join ranking)

**Files:**
- Create: `gateway/webspec_registry/resolver.py`
- Test: `gateway/tests/test_registry_resolver.py` (create)

**Interfaces:**
- Consumes: `Matcher`, `LexicalMatcher`, `ToolRecord`.
- Produces: `Ranked(record, score, status)` and
  `resolve(query, catalog, status_of=None, matcher=None, top_k=10) -> list[Ranked]`, where
  `status_of(record) -> str` returns one of `"connected"|"keychain"|"available"|"unavailable"`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_resolver.py
from webspec_registry.resolver import resolve
from webspec_registry.records import ToolRecord

CATALOG = [
    ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive"),
    ToolRecord("email", "send_email", "Send an email", "send", "message", "sensitive"),
    ToolRecord("gdrive", "get_file", "Download a file", "read", "file", "open"),
]


def test_ranks_relevant_tool_first():
    results = resolve("fire off a note", CATALOG)
    assert results[0].record.noun == "message"
    assert results[0].record.service in {"slack", "email"}


def test_status_weight_orders_connected_over_keychain():
    def status_of(rec):
        return "connected" if rec.service == "email" else "keychain"
    results = resolve("send a message", CATALOG, status_of=status_of)
    assert results[0].record.service == "email"  # connected (1.0) beats keychain (0.8)


def test_unavailable_filtered_out():
    results = resolve("send a message", CATALOG, status_of=lambda r: "unavailable")
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_resolver.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `resolver.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from .matcher import LexicalMatcher, Matcher
from .records import ToolRecord

STATUS_WEIGHT = {"connected": 1.0, "keychain": 0.8, "available": 0.5, "unavailable": 0.0}


@dataclass
class Ranked:
    record: ToolRecord
    score: float
    status: str


def resolve(query, catalog, status_of=None, matcher: Matcher | None = None, top_k: int = 10):
    """Rank catalog tools for a query using matcher score × status weight (the three-way join)."""
    matcher = matcher or LexicalMatcher()
    status_of = status_of or (lambda r: "available")
    ranked: list[Ranked] = []
    for rec in catalog:
        raw = matcher.score(query, rec)
        if raw <= 0.0:
            continue
        status = status_of(rec)
        final = raw * STATUS_WEIGHT.get(status, 0.0)
        if final <= 0.0:
            continue
        ranked.append(Ranked(record=rec, score=final, status=status))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked[:top_k]

# TODO(C): recency_boost, preference_boost, per-user history — see docs/discovery/three-way-join.md.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_resolver.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/resolver.py gateway/tests/test_registry_resolver.py
git commit -m "feat(registry): intent resolver with three-way-join status weighting"
```

---

### Task 6: Keychain reader (metadata only)

**Files:**
- Create: `gateway/webspec_registry/keychain.py`
- Test: `gateway/tests/test_registry_keychain.py` (create)

**Interfaces:**
- Consumes: `AccountRecord`; an `op_client` object exposing `list_vaults()`, `list_items(vault)`
  (both return metadata dicts). **Must not** call `op_client.read()` or `op_client.get_item()`.
- Produces: `read_accounts(op_client) -> list[AccountRecord]`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_keychain.py
import pytest
from webspec_registry.keychain import read_accounts


class FakeOp:
    """Mimics op-auth. read()/get_item() raise if called — they must never be."""
    def __init__(self):
        self.read_called = False

    def list_vaults(self):
        return [{"id": "v1", "name": "Personal"}]

    def list_items(self, vault):
        return [{
            "id": "i1", "title": "OpenAI", "category": "API_CREDENTIAL",
            "urls": [{"href": "https://platform.openai.com"}],
            "updated_at": "2026-01-01T00:00:00Z",
            # a value MUST NOT appear in output even if present here
            "fields": [{"label": "api_key", "value": "sk-SECRET-123"}],
        }]

    def read(self, *a, **k):
        self.read_called = True
        raise AssertionError("read() must never be called by the keychain reader")

    def get_item(self, *a, **k):
        raise AssertionError("get_item() must never be called by the keychain reader")


def test_reads_metadata_into_account_records():
    op = FakeOp()
    accounts = read_accounts(op)
    assert len(accounts) == 1
    a = accounts[0]
    assert a.vault == "Personal" and a.title == "OpenAI"
    assert a.category == "API_CREDENTIAL"
    assert a.urls == ("https://platform.openai.com",)


def test_never_surfaces_secret_values():
    op = FakeOp()
    accounts = read_accounts(op)
    blob = repr(accounts)
    assert "sk-SECRET-123" not in blob
    assert op.read_called is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_keychain.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `keychain.py`**

```python
from __future__ import annotations

from .records import AccountRecord


def read_accounts(op_client) -> list[AccountRecord]:
    """Inventory 1Password using metadata only.

    Uses list_vaults() + list_items(vault). Deliberately NEVER calls read() or get_item(),
    so no secret field value ever enters this process. Field *values* are ignored even if a
    list_items payload includes them.
    """
    accounts: list[AccountRecord] = []
    for vault in op_client.list_vaults():
        vname = vault.get("name") or vault.get("id") or ""
        for item in op_client.list_items(vname):
            urls = tuple(u.get("href", "") for u in item.get("urls", []) if u.get("href"))
            # field *labels* only — never values
            fields_present = tuple(
                f.get("label") or f.get("id") or "" for f in item.get("fields", [])
            )
            accounts.append(AccountRecord(
                vault=vname,
                item=item.get("id") or item.get("title") or "",
                title=item.get("title", ""),
                category=item.get("category", ""),
                urls=urls,
                fields_present=tuple(fp for fp in fields_present if fp),
                updated_at=item.get("updated_at"),
            ))
    return accounts

# TODO(C): balance/quota/liveness enrichment — per-provider billing calls to answer
# "does this API key still have credit / an active account". One adapter per provider.
# TODO(C): link_service(account, catalog) matching by URL/title to a gateway service.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_keychain.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/keychain.py gateway/tests/test_registry_keychain.py
git commit -m "feat(registry): metadata-only 1Password keychain reader"
```

---

### Task 7: Graph + poset

**Files:**
- Create: `gateway/webspec_registry/graph.py`
- Test: `gateway/tests/test_registry_graph.py` (create)

**Interfaces:**
- Consumes: `ToolRecord`, `AccountRecord`.
- Produces: `Graph(nodes, edges)`; `build_graph(catalog, accounts=()) -> Graph`;
  `to_dot(graph) -> str`; `poset(catalog) -> dict[tuple[str, str], int]` (a (verb,noun)→max-tier-rank map).

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_graph.py
from webspec_registry.graph import build_graph, to_dot, poset
from webspec_registry.records import ToolRecord, AccountRecord

CATALOG = [
    ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive"),
    ToolRecord("op-auth", "run", "Run op", "invoke", "run", "dangerous"),
]
ACCOUNTS = [AccountRecord("Personal", "i1", "Slack", "LOGIN", linked_service="slack")]


def test_graph_has_service_noun_verb_nodes():
    g = build_graph(CATALOG, ACCOUNTS)
    ids = {n["id"] for n in g.nodes}
    assert "service:slack" in ids and "noun:message" in ids and "verb:send" in ids
    assert "account:Personal/i1" in ids


def test_graph_links_account_to_service():
    g = build_graph(CATALOG, ACCOUNTS)
    assert any(e["src"] == "account:Personal/i1" and e["dst"] == "service:slack"
               and e["rel"] == "credentials" for e in g.edges)


def test_to_dot_renders():
    dot = to_dot(build_graph(CATALOG))
    assert dot.startswith("digraph webspec {")
    assert "->" in dot and dot.rstrip().endswith("}")


def test_poset_ranks_by_tier():
    p = poset(CATALOG)
    assert p[("send", "message")] == 1   # sensitive
    assert p[("invoke", "run")] == 2     # dangerous
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_graph.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `graph.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

TIER_RANK = {"open": 0, "sensitive": 1, "dangerous": 2}


@dataclass
class Graph:
    nodes: list[dict]  # {id, type, label}
    edges: list[dict]  # {src, dst, rel}


def build_graph(catalog, accounts=()) -> Graph:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(nid: str, ntype: str, label: str | None = None) -> None:
        nodes.setdefault(nid, {"id": nid, "type": ntype, "label": label or nid})

    for rec in catalog:
        s, v, n, t = (f"service:{rec.service}", f"verb:{rec.verb}",
                      f"noun:{rec.noun}", f"tier:{rec.tier}")
        add(s, "service"); add(v, "verb"); add(n, "noun"); add(t, "tier")
        edges.append({"src": s, "dst": n, "rel": "exposes"})
        edges.append({"src": n, "dst": v, "rel": rec.tool})
        edges.append({"src": v, "dst": t, "rel": "tier"})

    for acc in accounts:
        aid = f"account:{acc.vault}/{acc.item}"
        add(aid, "account", acc.title)
        if acc.linked_service:
            add(f"service:{acc.linked_service}", "service")
            edges.append({"src": aid, "dst": f"service:{acc.linked_service}", "rel": "credentials"})

    return Graph(nodes=list(nodes.values()), edges=edges)


def to_dot(graph: Graph) -> str:
    lines = ["digraph webspec {"]
    for nd in graph.nodes:
        lines.append(f'  "{nd["id"]}" [label="{nd["label"]}"];')
    for e in graph.edges:
        lines.append(f'  "{e["src"]}" -> "{e["dst"]}" [label="{e["rel"]}"];')
    lines.append("}")
    return "\n".join(lines)


def poset(catalog) -> dict[tuple[str, str], int]:
    """(verb, noun) → highest tier rank observed. The ordering relation is open ⊑ sensitive ⊑ dangerous."""
    out: dict[tuple[str, str], int] = {}
    for rec in catalog:
        key = (rec.verb, rec.noun)
        out[key] = max(out.get(key, 0), TIER_RANK[rec.tier])
    return out

# TODO(C): noun-containment ordering (message ⊑ channel ⊑ workspace) for a richer poset —
# requires the noun hierarchy sketched in docs/url-grammar/object-type-system.md.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_graph.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/graph.py gateway/tests/test_registry_graph.py
git commit -m "feat(registry): services→nouns→verbs graph, DOT export, tier poset"
```

---

### Task 8: Registry service (localhost-only) + entrypoint + ROADMAP-C

**Files:**
- Create: `gateway/webspec_registry/app.py`, `gateway/webspec_registry/__main__.py`
- Modify: `docs/ROADMAP-C.md` (append the registry `TODO(C)` items)
- Test: `gateway/tests/test_registry_service.py` (create)

**Interfaces:**
- Consumes: `harvest`, `resolve`, `build_graph`, `to_dot`, `read_accounts`.
- Produces: `create_registry_app(catalog_fn, accounts_fn=lambda: []) -> Starlette` with routes
  `GET /resolve?q=`, `GET /catalog`, `GET /graph[?format=dot]`; `main()` for `python -m webspec_registry`.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_registry_service.py
from starlette.testclient import TestClient
from webspec_registry.app import create_registry_app
from webspec_registry.records import ToolRecord, AccountRecord

CATALOG = [ToolRecord("slack", "send_message", "Send a message", "send", "message", "sensitive")]
ACCOUNTS = [AccountRecord("Personal", "i1", "Slack", "LOGIN", linked_service="slack")]


def _client():
    app = create_registry_app(catalog_fn=lambda: CATALOG, accounts_fn=lambda: ACCOUNTS)
    return TestClient(app)


def test_resolve_endpoint():
    r = _client().get("/resolve", params={"q": "send a message"})
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["service"] == "slack"


def test_catalog_endpoint():
    r = _client().get("/catalog")
    assert r.status_code == 200
    assert r.json()["tools"][0]["tool"] == "send_message"


def test_graph_dot_endpoint():
    r = _client().get("/graph", params={"format": "dot"})
    assert r.status_code == 200
    assert r.text.startswith("digraph webspec {")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd gateway && python -m pytest tests/test_registry_service.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `app.py` and `__main__.py`**

`gateway/webspec_registry/app.py`:

```python
from __future__ import annotations

from dataclasses import asdict

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .graph import build_graph, to_dot
from .resolver import resolve


def create_registry_app(catalog_fn, accounts_fn=lambda: []) -> Starlette:
    """Build the registry service. catalog_fn() -> list[ToolRecord]; accounts_fn() -> list[AccountRecord].

    NOTE (tier B): this app is localhost-only. Exposing it publicly requires porting the gateway
    guard middleware — TODO(C).
    """
    async def resolve_ep(request):
        q = request.query_params.get("q", "")
        results = resolve(q, catalog_fn())
        return JSONResponse({"query": q, "results": [
            {"service": r.record.service, "tool": r.record.tool, "verb": r.record.verb,
             "noun": r.record.noun, "tier": r.record.tier, "status": r.status,
             "score": round(r.score, 3)} for r in results]})

    async def catalog_ep(request):
        return JSONResponse({"tools": [asdict(r) for r in catalog_fn()]})

    async def graph_ep(request):
        g = build_graph(catalog_fn(), accounts_fn())
        if request.query_params.get("format") == "dot":
            return PlainTextResponse(to_dot(g))
        return JSONResponse({"nodes": g.nodes, "edges": g.edges})

    return Starlette(routes=[
        Route("/resolve", resolve_ep, methods=["GET"]),
        Route("/catalog", catalog_ep, methods=["GET"]),
        Route("/graph", graph_ep, methods=["GET"]),
    ])
```

`gateway/webspec_registry/__main__.py`:

```python
from __future__ import annotations

import os

import uvicorn

from .app import create_registry_app
from .catalog import harvest, http_fetch_tools


def _default_catalog():
    gateway_url = os.environ.get("WEBSPEC_GATEWAY_URL", "http://localhost:7002")
    # Discover service names from the gateway index, then harvest each.
    import json
    import urllib.request
    with urllib.request.urlopen(gateway_url.rstrip("/") + "/", timeout=5) as resp:
        services = [s["name"] for s in json.loads(resp.read()).get("services", [])]
    return harvest(services, http_fetch_tools(gateway_url))


def main() -> None:
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", "7003"))
    app = create_registry_app(catalog_fn=_default_catalog)
    # localhost-only for tier B (see create_registry_app note).
    uvicorn.run(app, host=os.environ.get("WEBSPEC_HOST", "127.0.0.1"), port=port)


if __name__ == "__main__":
    main()
```

Append to `docs/ROADMAP-C.md` (created in Track A Task 8; if running this plan first, create it):

```markdown
## Registry (from the registry plan)
- EmbeddingMatcher / multi-vector semantic search (matcher.py) — spec §5.1
- recency & preference boosts, per-user history (resolver.py) — spec §5.1
- balance/quota/liveness enrichment per provider (keychain.py) — spec §5.2
- account→service linking by URL/title (keychain.py) — spec §5.2
- noun-containment poset ordering (graph.py) — spec §5.2
- public exposure of the registry service via ported guard middleware (app.py) — spec §3.4
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd gateway && python -m pytest tests/test_registry_service.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gateway/webspec_registry/app.py gateway/webspec_registry/__main__.py docs/ROADMAP-C.md gateway/tests/test_registry_service.py
git commit -m "feat(registry): localhost-only service exposing resolve/catalog/graph"
```

---

## Self-Review

- **Spec coverage:** Trunk §4 → Tasks 1-3; B1 resolver §5.1 → Tasks 4-5; B2 keychain graph §5.2 →
  Tasks 6-7; service exposure → Task 8. The keychain metadata-only invariant (§5.2) is enforced by
  `test_registry_keychain.py`.
- **Placeholder scan:** all C items are real `# TODO(C):` markers with a `docs/ROADMAP-C.md` line, not
  vague gaps; every code step shows complete code.
- **Type consistency:** `ToolRecord`/`AccountRecord` field names are fixed in Task 1 and used
  verbatim in Tasks 3, 4, 5, 6, 7, 8. `resolve(...)`, `harvest(...)`, `build_graph(...)`, `to_dot(...)`,
  `read_accounts(...)`, `create_registry_app(...)` signatures match between their defining task and
  every consumer.
- **Cross-plan dependency:** Track A Task 7's compose `registry` service runs `python -m
  webspec_registry` (this plan's Task 8). Either plan can be built first; the compose `registry`
  block only *runs* once this plan lands. `docs/ROADMAP-C.md` is created by whichever plan runs first.
- **op_client wiring:** the registry service consumes op-auth as its keychain source. For B, wire
  `accounts_fn` to an op-auth client in a follow-up when the deployment provides op-auth (documented
  in `docker/README.md`); tests inject a fake so the invariant is verified independently.
```
