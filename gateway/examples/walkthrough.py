"""Generate the transcripts in docs/guide/walkthrough.md.

Runs the real gateway in-process (Starlette's TestClient) in front of the real demo MCP
server (examples/demo_server.py, over stdio), once per level, and prints every exchange the
reference shim (examples/shim.py) makes.

    cd gateway && python examples/walkthrough.py > walkthrough-transcripts.md

Level 4 needs ``ssh-keygen``. Here the approval is signed with ``ssh-keygen -Y sign``
directly — the same signature ``webspec-ctl approve`` makes after a person has read the
summary and typed ``approve`` on their terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from shim import Exchange, Shim  # noqa: E402

DEMO_KEY = bytes.fromhex("5e" * 32)  # a fixed demo guard key; real keys come from a vault
HOST, DESTINATION = "notes.localhost", "notes"
NOTE = {"id": "welcome", "to": "ana@example.com"}

SHOWN_REQUEST = ["Host", "Content-Type", "X-Gimme-Definer", "Idempotency-Key", "X-UFO-Clearance",
                 "X-WebSpec-Approval", "X-WebSpec-Nonce", "X-WebSpec-Guard"]
SHOWN_RESPONSE = ["allow", "cache-control", "idempotent-replayed", "x-webspec-level", "x-webspec-tier",
                  "x-ufo-taint", "x-gimme-definer-canonical", "x-gimme-definer-tier", "retry-after"]
DISPLAY = {"x-webspec-level": "X-WebSpec-Level", "x-webspec-tier": "X-WebSpec-Tier",
           "x-ufo-taint": "X-UFO-Taint", "idempotent-replayed": "Idempotent-Replayed",
           "x-gimme-definer-canonical": "X-Gimme-Definer-Canonical",
           "x-gimme-definer-tier": "X-Gimme-Definer-Tier"}


def make_approver(workdir: Path) -> tuple[Path, Path]:
    key = workdir / "approver"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ana", "-f", str(key)], check=True)
    kind, blob = (workdir / "approver.pub").read_text().split()[:2]
    signers = workdir / "allowed_signers"
    signers.write_text(f'ana@example.com namespaces="webspec-approval" {kind} {blob}\n')
    return key, signers


def approve(key: Path, challenge: dict) -> str:
    """What `webspec-ctl approve` does once the person has confirmed."""
    armored = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", "webspec-approval", "-f", str(key)],
                             input=challenge["sign_message"].encode(), capture_output=True,
                             check=True).stdout.decode()
    blob = "".join(ln for ln in armored.splitlines() if ln and not ln.startswith("-----"))
    return f"{challenge['challenge']}:{blob}"


def scenario(level: int, workdir: Path) -> list[tuple[str, list[Exchange]]]:
    """Run one level's story; return (caption, exchanges) steps."""
    from starlette.testclient import TestClient

    import webspec.app as appmod
    from webspec import approval, guard, handlers, idempotency
    from webspec.methods import ContractPins

    # Each level is a fresh gateway process: nothing one-time carries over between levels.
    handlers.contract_pins = ContractPins()
    idempotency.store = idempotency.IdempotencyStore()
    approval.store = approval.ApprovalStore()
    guard.spent_clearances = guard._SpentClearances()

    config = workdir / f"config-{level}.json"
    config.write_text(json.dumps({"mcpServers": {DESTINATION: {
        "command": sys.executable, "args": [str(HERE / "demo_server.py")], "level": level}}}))
    os.environ["WEBSPEC_CONFIG"] = str(config)
    os.environ["WEBSPEC_GUARD_KEY"] = DEMO_KEY.hex()
    os.environ["WEBSPEC_AUDIT_LOG"] = str(workdir / "audit.jsonl")
    os.environ.pop("WEBSPEC_DOMAIN", None)
    if level >= 4:
        approver_key, signers = make_approver(workdir)
        os.environ["WEBSPEC_APPROVERS_FILE"] = str(signers)

    steps: list[tuple[str, list[Exchange]]] = []
    with TestClient(appmod.create_app()) as http:
        shim = Shim(http, HOST, DESTINATION, DEMO_KEY)

        def step(caption: str, run) -> object:
            start = len(shim.log)
            result = run()
            shown = [x for x in shim.log[start:] if x.target != "/__nonce" or caption.startswith("Nonce")]
            steps.append((caption, shown))
            return result

        shim.discover("read_note"), shim.discover("send_note"), shim.discover("delete_note")

        if level == 0:
            step("Read", lambda: shim.call("GET", "read_note", {"id": "welcome"}))
            step("A read can't reach a delete", lambda: shim.call("GET", "delete_note", {"id": "welcome"}))
            step("Send", lambda: shim.call("POST", "send_note", NOTE, "SEND"))
            step("Delete", lambda: shim.call("DELETE", "delete_note", {"id": "welcome"}, "REMOVE"))
        elif level == 1:
            step("Nonce", lambda: shim._nonce())
            step("Read, signed", lambda: shim.call("GET", "read_note", {"id": "welcome"}))
            last = shim.log[-1]
            step("The same request again", lambda: shim._send(last.method, last.target.split("?")[0],
                                                              last.target.partition("?")[2], last.body,
                                                              {k: v for k, v in last.headers.items() if k != "Host"}))
        elif level == 2:
            step("Send", lambda: shim.call("POST", "send_note", NOTE, "SEND", idempotency_key="k-7f3a"))
            step("Retry with the same key", lambda: shim.call("POST", "send_note", NOTE, "SEND",
                                                              idempotency_key="k-7f3a"))
            step("Same key, different request", lambda: shim.call(
                "POST", "send_note", {**NOTE, "to": "bo@example.com"}, "SEND", idempotency_key="k-7f3a"))
        elif level == 3:
            shim.vouch = lambda method, tool, args: False
            step("Send, not vouched for", lambda: shim.call("POST", "send_note", NOTE, "SEND"))
            shim.vouch = lambda method, tool, args: True
            step("Send, vouched for", lambda: shim.call("POST", "send_note", NOTE, "SEND"))
            step("Read (tier open: no clearance needed)", lambda: shim.call("GET", "read_note", {"id": "welcome"}))
        else:
            step("Read (no approval needed)", lambda: shim.call("GET", "read_note", {"id": "welcome"}))
            first = step("Send", lambda: shim.call("POST", "send_note", NOTE, "SEND", idempotency_key="k-91c2"))
            header = approve(approver_key, first.json())
            step("Send again, with the approval", lambda: shim.call("POST", "send_note", NOTE, "SEND",
                                                                    approval=header, idempotency_key="k-91c2"))
            first = step("Delete", lambda: shim.call("DELETE", "delete_note", {"id": "welcome"}, "REMOVE"))
            header = approve(approver_key, first.json())
            step("Delete again, with the approval", lambda: shim.call("DELETE", "delete_note", {"id": "welcome"},
                                                                      "REMOVE", approval=header))
    return steps


def _short(value: str, keep: int = 24) -> str:
    return value if len(value) <= keep + 8 else value[:keep] + "…"


def render(exchange: Exchange) -> str:
    lines = [f"{exchange.method} {exchange.target}"]
    for name in SHOWN_REQUEST:
        if name in exchange.headers:
            value = exchange.headers[name]
            lines.append(f"{name}: {_short(value) if name == 'X-WebSpec-Approval' else value}")
    if exchange.body:
        lines += ["", exchange.body.decode()]
    lines += ["", f"→ {exchange.status} {HTTPStatus(exchange.status).phrase}"]
    for name in SHOWN_RESPONSE:
        if name in exchange.response_headers:
            lines.append(f"{DISPLAY.get(name, name.title())}: {exchange.response_headers[name]}")
    if exchange.response_body:
        try:
            data = json.loads(exchange.response_body)
        except ValueError:
            text = exchange.response_body.decode()
        else:
            if isinstance(data, dict) and "summary" in data:  # a 428 challenge: keep it readable
                data = {k: data[k] for k in ("error", "challenge", "fingerprint", "summary", "expires_in")}
                text = json.dumps(data, indent=2)
            else:
                text = json.dumps(data)
        lines += ["", text]
    return "\n".join(lines)


def main() -> None:
    for level in range(5):
        with tempfile.TemporaryDirectory(prefix="webspec-walkthrough-") as tmp:
            steps = scenario(level, Path(tmp))
        print(f"<!-- level {level} -->")
        for caption, exchanges in steps:
            print(f"**{caption}**\n")
            for exchange in exchanges:
                print("```text\n" + render(exchange) + "\n```\n")


if __name__ == "__main__":
    main()
