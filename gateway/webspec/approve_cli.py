"""``webspec-ctl approve``: a human signs a level-4 approval challenge.

What you see is what you sign: the fingerprint is re-derived from the summary shown on
screen, so a tampered challenge (benign summary, malicious fingerprint) is refused. The
summary is printed with every non-ASCII and control character escaped, so ANSI escapes,
bidi overrides, or homoglyphs in agent-supplied arguments cannot hide anything.

Confirmation is read from the controlling terminal (``/dev/tty``), never from stdin, so
a non-interactive agent shell cannot answer it. The signature itself is made by
``ssh-keygen -Y sign`` (or a compatible signer such as 1Password's ``op-ssh-sign``)
with a key whose private half should live in an agent that demands a biometric gesture.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .approval import APPROVAL_NAMESPACE, dearmor, sign_message, summary_fingerprint

SIGN_TIMEOUT = 120.0


def _render(summary: dict) -> str:
    return json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True)


def _confirm(prompt: str) -> bool:
    """Ask on the controlling terminal. Raises OSError if there is none."""
    with open("/dev/tty", "r+", encoding="utf-8") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().strip() == "approve"


def _show(text: str) -> None:
    try:
        with open("/dev/tty", "w", encoding="utf-8") as tty:
            tty.write(text)
    except OSError:
        sys.stderr.write(text)


def cmd_approve(args: argparse.Namespace) -> int:
    raw = Path(args.challenge).read_text() if args.challenge else sys.stdin.read()
    try:
        challenge = json.loads(raw)
        cid = challenge["challenge"]
        fingerprint = challenge["fingerprint"]
        summary = challenge["summary"]
    except (json.JSONDecodeError, KeyError, TypeError):
        print("error: not a WebSpec approval challenge (expected the 428 response body)", file=sys.stderr)
        return 2
    if not isinstance(cid, str) or not cid.isalnum():
        print("error: malformed challenge id", file=sys.stderr)
        return 2

    expected_message = sign_message(cid, fingerprint)
    if summary_fingerprint(summary) != fingerprint or challenge.get("sign_message") != expected_message:
        print("REFUSED: the challenge's fingerprint does not match its summary (tampered challenge).",
              file=sys.stderr)
        return 2

    _show("\n=== WebSpec level-4 approval request ===\n" + _render(summary) +
          f"\nfingerprint: {fingerprint}\n\n")
    try:
        ok = _confirm("Type 'approve' to sign this exact request: ")
    except OSError:
        print("error: approval requires an interactive terminal (/dev/tty)", file=sys.stderr)
        return 2
    if not ok:
        print("Not approved.", file=sys.stderr)
        return 1

    key = args.key or os.environ.get("WEBSPEC_APPROVER_KEY")
    if not key:
        print("error: no signing key (use --key or WEBSPEC_APPROVER_KEY)", file=sys.stderr)
        return 2
    signer = args.signer or os.environ.get("WEBSPEC_APPROVAL_SIGNER") or "ssh-keygen"
    try:
        proc = subprocess.run(
            [signer, "-Y", "sign", "-n", APPROVAL_NAMESPACE, "-f", key],
            input=expected_message.encode(),
            capture_output=True,
            timeout=SIGN_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"error: signer failed: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"error: signer exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
        return 1

    blob = dearmor(proc.stdout.decode())
    print(f"X-WebSpec-Approval: {cid}:{blob}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("approve", help="Sign a level-4 approval challenge (human only)")
    p.add_argument("challenge", nargs="?", help="File with the 428 challenge JSON (default: stdin)")
    p.add_argument("--key", help="SSH key file to sign with — a .pub file uses ssh-agent/1Password "
                                 "(env WEBSPEC_APPROVER_KEY)")
    p.add_argument("--signer", help="ssh-keygen -Y sign compatible program, e.g. op-ssh-sign "
                                    "(env WEBSPEC_APPROVAL_SIGNER; default ssh-keygen)")
