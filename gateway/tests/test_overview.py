"""The overview's answers come from the real gateway; keep them true.

docs/index.md opens with a delete as the reference gateway received it, then quotes what the
gateway answers to requests that misdescribe themselves. These tests send those requests to the
real gateway in front of the real demo MCP server (stdio, no mocks), as test_examples.py does
for the walkthrough, and check that the page quotes only answers seen here.
"""

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DOCS = Path(__file__).resolve().parents[2] / "docs"
sys.path.insert(0, str(EXAMPLES))
import walkthrough  # noqa: E402
from shim import Shim, guard_tag  # noqa: E402

PAGE = (DOCS / "index.md").read_text()
KEY, NOTE = walkthrough.DEMO_KEY, walkthrough.NOTE
OTHER_ORIGIN = "https://evil.example"
# What an HTML form on another origin can post without a preflight: one body per encoding.
FORMS = {
    "text/plain": json.dumps({**NOTE, "to": "eve@example.com"}).encode(),
    "application/x-www-form-urlencoded": b"id=welcome&to=eve%40example.com",
    "multipart/form-data; boundary=b": b'--b\r\nContent-Disposition: form-data; name="id"\r\n\r\nwelcome\r\n--b--\r\n',
}


def answer(response) -> tuple[int, str | None]:
    """The status and the error code of a gateway answer."""
    try:
        return response.status_code, response.json().get("error")
    except ValueError:
        return response.status_code, None


@pytest.fixture(scope="module")
def sent(tmp_path_factory):
    """Every request the page describes, sent once to the real gateway: {what: response}."""
    from starlette.testclient import TestClient

    import webspec.app as appmod
    from webspec import approval, guard, handlers, idempotency
    from webspec.methods import ContractPins

    workdir = tmp_path_factory.mktemp("overview")
    got = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WEBSPEC_GUARD_KEY", KEY.hex())
        mp.setenv("WEBSPEC_AUDIT_LOG", str(workdir / "audit.jsonl"))
        for var in ("WEBSPEC_DOMAIN", "WEBSPEC_CORS_ORIGINS"):
            mp.delenv(var, raising=False)

        @contextmanager
        def gateway(level: int, *destinations: str):
            # A fresh gateway process: nothing one-time carries over.
            mp.setattr(handlers, "contract_pins", ContractPins())
            mp.setattr(idempotency, "store", idempotency.IdempotencyStore())
            mp.setattr(approval, "store", approval.ApprovalStore())
            mp.setattr(guard, "spent_clearances", guard._SpentClearances())
            server = {"command": sys.executable, "args": [str(EXAMPLES / "demo_server.py")], "level": level}
            config = workdir / f"config-{level}.json"
            config.write_text(json.dumps({"mcpServers": {d: server for d in destinations}}))
            mp.setenv("WEBSPEC_CONFIG", str(config))
            with TestClient(appmod.create_app()) as http:
                yield http

        with gateway(0, "notes") as http:
            notes = Shim(http, "notes.localhost", "notes", KEY)
            here = {"Host": "notes.localhost"}
            got["unserved host"] = http.get("/read_note?id=welcome", headers={"Host": "mail.localhost"})
            got["GET of a delete"] = notes.call("GET", "delete_note", {"id": "welcome"})
            got["preflight"] = http.options("/send_note", headers={
                **here, "Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-gimme-definer"})
            for content_type, body in FORMS.items():
                got[f"form, {content_type}"] = http.post("/send_note", content=body, headers={
                    **here, "Content-Type": content_type, "Origin": OTHER_ORIGIN})
            got["DELETE verb on a POST"] = notes.call("POST", "send_note", NOTE, "REMOVE")
            got["repeated query key"] = http.get("/read_note?id=welcome&id=other", headers=here)
            got["repeated JSON key"] = http.post(
                "/send_note", content=b'{"id":"welcome","to":"ana@example.com","to":"eve@example.com"}',
                headers={**here, "Content-Type": "application/json", "X-Gimme-Definer": "SEND"})
            got["GET from another origin"] = http.get("/read_note?id=welcome", headers={**here, "Origin": OTHER_ORIGIN})
            http.head("/delete_note?id=welcome", headers=here)
            http.options("/delete_note", headers=here)
            got["read after HEAD and OPTIONS"] = http.get("/read_note?id=welcome", headers=here)
            # Last at this level: it deletes the note.
            got["PUT with a PUT verb"] = http.put("/delete_note", content=b'{"id":"welcome"}', headers={
                **here, "Content-Type": "application/json", "X-Gimme-Definer": "SET"})
            got["read after the PUT"] = http.get("/read_note?id=welcome", headers=here)

        with gateway(1, "notes", "mail") as http:
            notes = Shim(http, "notes.localhost", "notes", KEY)
            to_ana = json.dumps(NOTE, separators=(",", ":")).encode()
            to_eve = json.dumps({**NOTE, "to": "eve@example.com"}, separators=(",", ":")).encode()

            def signed(body: bytes, sent_to: str, sent_body: bytes):
                """A send signed for notes with body, then sent to sent_to with sent_body."""
                nonce = notes._nonce()
                tag = guard_tag(KEY, "POST", "notes.localhost", "/send_note", nonce, body, "", "SEND")
                return http.post("/send_note", content=sent_body, headers={
                    "Host": sent_to, "Content-Type": "application/json", "X-Gimme-Definer": "SEND",
                    "X-WebSpec-Nonce": nonce, "X-WebSpec-Guard": tag})

            got["signed send"] = signed(to_ana, "notes.localhost", to_ana)
            got["re-addressed"] = signed(to_ana, "notes.localhost", to_eve)
            got["sent to another destination"] = signed(to_ana, "mail.localhost", to_ana)
            got["shim's send"] = notes.call("POST", "send_note", NOTE, "SEND")
            last = notes.log[-1]
            got["same bytes again"] = notes._send(last.method, last.target, "", last.body,
                                                  {k: v for k, v in last.headers.items() if k != "Host"})
    return got


@pytest.mark.parametrize("request_text", [
    "DELETE /delete_note?id=welcome\nHost: notes.localhost\nX-Gimme-Definer: REMOVE\n",
    "GET /read_note?id=welcome\nHost: notes.localhost\nX-WebSpec-Nonce: 403c28aba42fa462a23469b97db9b204\n"
    "X-WebSpec-Guard: ef56efa3\n",
], ids=["the delete", "the signed read"])
def test_the_requests_on_the_page_are_the_walkthroughs(request_text):
    # test_examples.py replays them: the delete gets 200, the signed read 200 and then 403 nonce_reused.
    assert request_text in PAGE
    assert request_text in (DOCS / "guide" / "walkthrough.md").read_text()


def test_a_host_the_gateway_does_not_serve(sent):
    assert answer(sent["unserved host"]) == (404, "unknown_service")


def test_a_get_cannot_reach_a_delete(sent):
    response = sent["GET of a delete"]
    assert answer(response)[0] == 405 and response.headers["allow"] == "HEAD, OPTIONS, PUT, DELETE"
    assert "`405 Method Not Allowed` with `Allow: HEAD, OPTIONS, PUT, DELETE`" in PAGE


def test_another_origin_cannot_add_the_definer(sent):
    # A page on another origin needs a preflight to add the header, and the gateway refuses it.
    assert sent["preflight"].status_code == 400
    assert "access-control-allow-origin" not in sent["preflight"].headers
    # A form needs none, and arrives without a definer, whatever its encoding.
    for content_type in FORMS:
        assert answer(sent[f"form, {content_type}"]) == (400, "missing_definer"), content_type


def test_another_origin_can_run_a_read_but_cannot_see_the_answer(sent):
    response = sent["GET from another origin"]
    assert response.status_code == 200 and "access-control-allow-origin" not in response.headers


def test_head_and_options_never_run_a_tool(sent):
    assert sent["read after HEAD and OPTIONS"].json()["result"] == "Hello from WebSpec."


def test_a_verb_from_another_methods_family(sent):
    assert answer(sent["DELETE verb on a POST"]) == (400, "definer_family_mismatch")


def test_the_definer_is_checked_against_the_method_not_the_tool(sent):
    # A PUT to the delete, with a PUT verb, deletes the note.
    response = sent["PUT with a PUT verb"]
    assert answer(response) == (200, None) and response.json()["result"] == "deleted"
    assert sent["read after the PUT"].json()["result"] == ""


def test_each_query_key_once(sent):
    assert answer(sent["repeated query key"]) == (400, "duplicate_query_key")


def test_a_repeated_json_key_is_not_refused_yet(sent):
    # AR-4: the last one wins. The page says so; once the gateway refuses these, it should say that.
    response = sent["repeated JSON key"]
    assert response.status_code == 200 and response.json()["result"]["to"] == "eve@example.com"
    assert "Repeated keys inside a JSON body are not refused yet: the last one wins." in PAGE


def test_a_signature_covers_the_request_and_its_destination(sent):
    assert answer(sent["signed send"]) == (200, None)
    assert answer(sent["re-addressed"]) == (403, "guard_invalid")
    assert answer(sent["sent to another destination"]) == (403, "guard_invalid")


def test_the_same_signed_bytes_twice(sent):
    assert answer(sent["shim's send"]) == (200, None)
    assert answer(sent["same bytes again"]) == (403, "nonce_reused")


def test_the_page_quotes_only_answers_seen_here(sent):
    seen = {f"{status} {error}" for status, error in map(answer, sent.values()) if error}
    quoted = set(re.findall(r"`([1-5]\d\d [a-z_]+)`", PAGE))
    assert quoted, "the page quotes no answers"
    assert quoted <= seen, f"quoted on the page but not seen here: {sorted(quoted - seen)}"
