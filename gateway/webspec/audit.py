"""Tamper-evident, URL-addressed audit log of every tool invocation attempt.

One JSON line per decision (invoked / denied / challenged / replayed), each carrying the
SHA-256 of the previous line (``prev``), so deleting or editing any line breaks the
chain from that point on. Arguments are recorded as a hash, never verbatim (they may
contain secrets); the URL, method, tool, contract, level, and outcome are recorded in
the clear, because "what was asked of whom" is exactly what an auditor needs.

Path: ``WEBSPEC_AUDIT_LOG`` (default ``~/.webspec/gateway-audit.jsonl``). Set it to an
empty string to disable. For real tamper-resistance (not just evidence), run the
gateway as a different OS user than the agent and/or ship lines off-host — an agent
with write access to the file can rewrite the whole chain consistently.

Failures to write are logged and never break a request (availability over audit at
this tier); TODO(C): fail-closed audit for levels >= 3 with an off-host sink.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .methods import ToolContract

logger = logging.getLogger("webspec.audit")

GENESIS = "0" * 64

_lock = threading.Lock()
_state: dict[str, str] = {}  # path -> hash of last line written by this process


@dataclass(frozen=True)
class Context:
    service: str
    host: str
    method: str
    path: str
    tool: str
    level: int
    contract: ToolContract
    query: str
    body: bytes


def _log_path() -> Path | None:
    raw = os.environ.get("WEBSPEC_AUDIT_LOG")
    if raw is None:
        return Path.home() / ".webspec" / "gateway-audit.jsonl"
    return Path(raw) if raw.strip() else None


def _last_hash(path: Path) -> str:
    """Hash of the file's last line (so the chain survives restarts)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return GENESIS
            f.seek(max(0, size - 65536))
            lines = f.read().splitlines()
            return hashlib.sha256(lines[-1]).hexdigest() if lines else GENESIS
    except FileNotFoundError:
        return GENESIS


def record(ctx: Context, *, outcome: str, status: int | None, reason: str | None, **extra) -> None:
    path = _log_path()
    if path is None:
        return
    entry = {
        "ts": round(time.time(), 3),
        "service": ctx.service,
        "host": ctx.host,
        "method": ctx.method,
        "path": ctx.path,
        "query_sha256": hashlib.sha256(ctx.query.encode()).hexdigest() if ctx.query else None,
        "body_sha256": hashlib.sha256(ctx.body).hexdigest() if ctx.body else None,
        "tool": ctx.tool,
        "level": ctx.level,
        "tier": ctx.contract.tier,
        "risk": ctx.contract.risk,
        "outcome": outcome,
        "status": status,
        "reason": reason,
        **{k: v for k, v in extra.items() if v is not None},
    }
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = str(path)
            prev = _state.get(key) or _last_hash(path)
            entry["prev"] = prev
            line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line + b"\n")
            finally:
                os.close(fd)
            _state[key] = hashlib.sha256(line).hexdigest()
    except OSError:
        logger.exception("Audit log write failed (%s)", path)


def verify_chain(path: Path) -> int | None:
    """Return the 1-based line number of the first broken link, or None if intact."""
    prev = GENESIS
    with path.open("rb") as f:
        for n, raw in enumerate(f, start=1):
            line = raw.rstrip(b"\n")
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return n
            if entry.get("prev") != prev:
                return n
            prev = hashlib.sha256(line).hexdigest()
    return None
