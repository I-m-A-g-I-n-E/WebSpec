"""Level 4 ("witnessed") human approval: challenge issuance + Ed25519/SSHSIG verification.

Flow (spec: docs/http-methods/method-profiles.md § Human approval):

1. A request that needs approval arrives without ``X-WebSpec-Approval`` → the gateway
   answers ``428 Precondition Required`` with a challenge whose ``fingerprint`` is the
   SHA-256 of a canonical summary of *exactly* the request it will accept.
2. A human runs ``webspec-ctl approve`` on the challenge. It re-derives the fingerprint
   from the displayed summary (what you see is what you sign), asks for confirmation on
   the controlling TTY, and signs ``sign_message`` with an SSH key in namespace
   ``webspec-approval`` — ideally a key held by 1Password's SSH agent so every signature
   costs a biometric gesture.
3. The client retries the identical request with ``X-WebSpec-Approval: <challenge>:<sig>``.
   The gateway verifies the signature with OpenSSH (``ssh-keygen -Y verify``) against
   the operator's allowed-signers file. Challenges are single-use and expire.

Signature verification is delegated to OpenSSH rather than re-implemented: code we
don't write. The gateway holds only *public* keys, so a compromised agent harness that
holds the guard key still cannot approve.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guard import canonical_json  # one canonical form for every MAC / fingerprint

logger = logging.getLogger("webspec.approval")

APPROVAL_NAMESPACE = "webspec-approval"
APPROVAL_TTL = 300  # seconds
SIGN_PREFIX = "webspec-approval/v1"
MAX_PENDING = 1000
MAX_FAILED_ATTEMPTS = 5
SSH_KEYGEN_TIMEOUT = 10.0

PENDING = "pending"
VERIFYING = "verifying"
USED = "used"




def request_summary(
    method: str, service: str, host: str, path: str, tool: str, args: dict, body: bytes
) -> dict[str, Any]:
    """Everything the approver is agreeing to. Hashing this binds approval to the request."""
    return {
        "v": 1,
        "method": method.upper(),
        "service": service,
        "host": host,
        "path": path,
        "tool": tool,
        "args": args,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def summary_fingerprint(summary: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(summary).encode()).hexdigest()


def sign_message(challenge_id: str, fingerprint: str) -> str:
    return f"{SIGN_PREFIX} {challenge_id} {fingerprint}"


def armor(blob_b64: str) -> str:
    lines = [blob_b64[i:i + 70] for i in range(0, len(blob_b64), 70)]
    return "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(lines) + "\n-----END SSH SIGNATURE-----\n"


def dearmor(armored: str) -> str:
    """Armored SSHSIG → single-line base64 blob (header-safe)."""
    body = [ln.strip() for ln in armored.strip().splitlines() if ln.strip() and not ln.startswith("-----")]
    return "".join(body)


@dataclass
class _Challenge:
    fingerprint: str
    created_at: float
    state: str = PENDING
    failures: int = 0


class ApprovalStore:
    def __init__(self, ttl: float = APPROVAL_TTL, max_pending: int = MAX_PENDING):
        self._ttl = ttl
        self._max = max_pending
        self._challenges: dict[str, _Challenge] = {}  # insertion order == age order

    def _purge(self, now: float) -> None:
        while self._challenges:
            cid, ch = next(iter(self._challenges.items()))
            if now - ch.created_at <= self._ttl:
                break
            del self._challenges[cid]

    def issue(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        """Challenge for this exact request, or None if the queue is full.

        Pending challenges are never evicted to make room — otherwise anyone holding the
        guard key could flush out the challenge a human is in the middle of signing. A
        retry of the same request gets the same pending challenge back.
        """
        now = time.monotonic()
        self._purge(now)
        fingerprint = summary_fingerprint(summary)
        cid = next((c for c, ch in self._challenges.items()
                    if ch.fingerprint == fingerprint and ch.state == PENDING), None)
        if cid is None:
            if len(self._challenges) >= self._max:
                return None
            cid = secrets.token_hex(16)
            self._challenges[cid] = _Challenge(fingerprint=fingerprint, created_at=now)
        return {
            "error": "approval_required",
            "detail": "This action requires human approval (level 4). "
                      "Sign the challenge with `webspec-ctl approve` and retry the identical "
                      "request with the X-WebSpec-Approval header it prints.",
            "challenge": cid,
            "fingerprint": fingerprint,
            "summary": summary,
            "sign_message": sign_message(cid, fingerprint),
            "namespace": APPROVAL_NAMESPACE,
            "expires_in": max(0, int(self._ttl - (now - self._challenges[cid].created_at))),
        }

    async def verify(self, header: str | None, fingerprint: str) -> str | None:
        """Return None if the header carries a valid, fresh, matching approval; else an error code."""
        if not header:
            return "approval_missing"
        cid, sep, blob = header.strip().partition(":")
        if not sep or not cid or not blob:
            return "approval_malformed"
        try:
            raw = base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            return "approval_malformed"
        if not raw.startswith(b"SSHSIG"):
            return "approval_malformed"

        ch = self._challenges.get(cid)
        if ch is None:
            return "approval_unknown"
        if time.monotonic() - ch.created_at > self._ttl:
            del self._challenges[cid]
            return "approval_expired"
        if ch.state == USED:
            return "approval_reused"
        if ch.state == VERIFYING:
            return "approval_in_progress"
        if not secrets.compare_digest(ch.fingerprint, fingerprint):
            return "approval_mismatch"

        approvers = os.environ.get("WEBSPEC_APPROVERS_FILE", "")
        keygen = os.environ.get("WEBSPEC_SSH_KEYGEN") or shutil.which("ssh-keygen")
        if not approvers or not Path(approvers).is_file() or not keygen:
            logger.error("Level-4 approval requested but WEBSPEC_APPROVERS_FILE/ssh-keygen unavailable")
            return "approval_unavailable"

        ch.state = VERIFYING  # exclusive: a concurrent retry can't double-spend this challenge
        ok = False
        try:
            ok = await _ssh_verify(keygen, approvers, blob, sign_message(cid, fingerprint))
        except Exception:
            logger.exception("Approval verification crashed")
        finally:
            # Runs on cancellation too, so a challenge can never be stranded in VERIFYING.
            if ok:
                ch.state = USED
            else:
                ch.failures += 1
                ch.state = PENDING
                if ch.failures >= MAX_FAILED_ATTEMPTS:
                    self._challenges.pop(cid, None)
        return None if ok else "approval_invalid"


async def _run(argv: list[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(stdin), timeout=SSH_KEYGEN_TIMEOUT)
    except BaseException as exc:  # timeout or cancellation: never orphan the child
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
        if isinstance(exc, asyncio.TimeoutError):
            return -1, b""
        raise
    return proc.returncode if proc.returncode is not None else -1, out


async def _ssh_verify(keygen: str, approvers: str, blob_b64: str, message: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="webspec-approval-") as tmp:
        sig_path = Path(tmp) / "approval.sig"
        sig_path.write_text(armor(blob_b64))
        os.chmod(sig_path, 0o600)

        rc, out = await _run([keygen, "-Y", "find-principals", "-s", str(sig_path), "-f", approvers])
        if rc != 0:
            return False
        principals = [p for p in out.decode(errors="replace").splitlines() if p.strip()]
        for principal in principals:
            rc, _ = await _run(
                [keygen, "-Y", "verify", "-f", approvers, "-I", principal,
                 "-n", APPROVAL_NAMESPACE, "-s", str(sig_path)],
                stdin=message.encode(),
            )
            if rc == 0:
                logger.info("Approval verified for principal %s", principal)
                return True
        return False


store = ApprovalStore()
