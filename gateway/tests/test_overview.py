"""The overview's answers come from the real gateway; keep them true.

The home page (docs/index.md) and How it works (docs/guide/how-it-works.md) show a delete as
the reference gateway received it, then quote what the gateway answers to requests that
misdescribe themselves or lack what a level requires. These tests send those requests to the
real gateway in front of the real demo MCP server (stdio, no mocks), as test_examples.py does
for the walkthrough, and check that the pages quote only answers seen here.
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
from shim import Shim, clearance, guard_tag  # noqa: E402

HOME = (DOCS / "index.md").read_text()
PAGE = (DOCS / "guide" / "how-it-works.md").read_text()
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

        _, signers = walkthrough.make_approver(workdir)
        mp.setenv("WEBSPEC_APPROVERS_FILE", str(signers))

        @contextmanager
        def gateway(level: int, *destinations: str):
            # A fresh gateway process: nothing one-time carries over.
            mp.setattr(handlers, "contract_pins", ContractPins())
            mp.setattr(handlers, "route_maps", {})
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
            got["unserved host"] = http.get("/note?id=welcome", headers={"Host": "mail.localhost"})
            got["unknown tool"] = http.get("/no_such_tool", headers=here)
            # /note maps GET to read_note and DELETE to delete_note. PUT is admissible for
            # delete_note under MB-1, but the map does not give it PUT (PA-5).
            got["a method /note doesn't map"] = notes.call("PUT", "/note", {"id": "welcome"})
            got["a send by GET"] = http.get("/email?id=welcome&to=eve%40example.com", headers=here)
            got["a verb /email doesn't map"] = notes.call("POST", "/email", NOTE, "CREATE")
            got["an id in the path"] = http.get("/note/welcome", headers=here)
            got["preflight"] = http.options("/email", headers={
                **here, "Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, webspec-definer"})
            for content_type, body in FORMS.items():
                got[f"form, {content_type}"] = http.post("/email", content=body, headers={
                    **here, "Content-Type": content_type, "Origin": OTHER_ORIGIN})
            got["DELETE verb on a POST"] = notes.call("POST", "/email", NOTE, "REMOVE")
            got["repeated query key"] = http.get("/note?id=welcome&id=other", headers=here)
            got["repeated JSON key"] = http.post(
                "/email", content=b'{"id":"welcome","to":"ana@example.com","to":"eve@example.com"}',
                headers={**here, "Content-Type": "application/json", "WebSpec-Definer": "SEND"})
            got["GET from another origin"] = http.get("/note?id=welcome", headers={**here, "Origin": OTHER_ORIGIN})
            http.head("/note?id=welcome", headers=here)
            http.options("/note", headers=here)
            got["read after HEAD and OPTIONS"] = http.get("/note?id=welcome", headers=here)
            # Last at this level: it deletes the note.
            got["DELETE with ARCHIVE"] = notes.call("DELETE", "/note", {"id": "welcome"}, "ARCHIVE")
            got["read after the DELETE"] = http.get("/note?id=welcome", headers=here)

        with gateway(1, "notes", "mail") as http:
            notes = Shim(http, "notes.localhost", "notes", KEY)
            to_ana = json.dumps(NOTE, separators=(",", ":")).encode()
            to_eve = json.dumps({**NOTE, "to": "eve@example.com"}, separators=(",", ":")).encode()

            def signed(body: bytes, sent_to: str, sent_body: bytes):
                """A send signed for notes with body, then sent to sent_to with sent_body."""
                nonce = notes._nonce()
                tag = guard_tag(KEY, "POST", "notes.localhost", "/email", nonce, body, "", "SEND")
                return http.post("/email", content=sent_body, headers={
                    "Host": sent_to, "Content-Type": "application/json", "WebSpec-Definer": "SEND",
                    "WebSpec-Nonce": nonce, "WebSpec-Guard": tag})

            got["signed send"] = signed(to_ana, "notes.localhost", to_ana)
            got["re-addressed"] = signed(to_ana, "notes.localhost", to_eve)
            got["sent to another destination"] = signed(to_ana, "mail.localhost", to_ana)
            got["shim's send"] = notes.call("POST", "/email", NOTE, "SEND")
            last = notes.log[-1]
            got["same bytes again"] = notes._send(last.method, last.target, "", last.body,
                                                  {k: v for k, v in last.headers.items() if k != "Host"})
            got["unsigned"] = http.get("/note?id=welcome", headers={"Host": "notes.localhost"})

        # The home page's dial: the send at each level, without what that level adds.
        body = json.dumps(NOTE, separators=(",", ":")).encode()
        with gateway(2, "notes") as http:
            notes = Shim(http, "notes.localhost", "notes", KEY)
            got["level 2, no key"] = notes._signed(2, "POST", "/email", body=body, headers={
                "Content-Type": "application/json", "WebSpec-Definer": "SEND"})
        with gateway(3, "notes") as http:
            unvouched = Shim(http, "notes.localhost", "notes", KEY, vouch=lambda *_: False)
            got["level 3, not vouched for"] = unvouched.call("POST", "/email", NOTE, "SEND")
        with gateway(4, "notes") as http:
            got["level 4, no approval"] = Shim(http, "notes.localhost", "notes", KEY).call("POST", "/email", NOTE, "SEND")
    return got


@pytest.mark.parametrize("request_text", [
    "DELETE /note?id=welcome\nHost: notes.localhost\n",
    "GET /note?id=welcome\nHost: notes.localhost\nWebSpec-Nonce: 4e77ef8d78b024923fb44820805d6547\n"
    "WebSpec-Guard: 2702dbea\n",
], ids=["the delete", "the signed read"])
def test_the_requests_on_the_page_are_the_walkthroughs(request_text):
    # test_examples.py replays them: the delete gets 200, the signed read 200 and then 403 nonce_reused.
    assert request_text in PAGE
    assert request_text in (DOCS / "guide" / "walkthrough.md").read_text()


def test_a_host_the_gateway_does_not_serve(sent):
    assert answer(sent["unserved host"]) == (404, "unknown_service")


def test_a_path_that_names_no_tool(sent):
    assert answer(sent["unknown tool"]) == (404, "tool_not_found")


def test_each_level_refuses_a_send_without_what_it_adds(sent):
    assert answer(sent["unsigned"]) == (401, "guard_missing")
    assert answer(sent["level 2, no key"]) == (400, "idempotency_key_required")
    assert answer(sent["level 3, not vouched for"]) == (403, "clearance_missing")
    assert answer(sent["level 4, no approval"]) == (428, "approval_required")


def test_a_method_the_path_does_not_map(sent):
    response = sent["a method /note doesn't map"]
    assert answer(response)[0] == 405 and response.headers["allow"] == "HEAD, OPTIONS, GET, DELETE"
    assert "`405 Method Not Allowed` with `Allow: HEAD, OPTIONS, GET, DELETE`" in PAGE
    assert "<code>PUT /note</code>:</span> <code>405 Method Not Allowed</code> with\n" \
           "<code>Allow: HEAD, OPTIONS, GET, DELETE</code>" in HOME


def test_a_get_cannot_reach_a_tool_that_is_not_read_only(sent):
    response = sent["a send by GET"]
    assert answer(response)[0] == 405 and response.headers["allow"] == "HEAD, OPTIONS, POST"
    assert "`GET /email` gets `405 Method Not Allowed` with `Allow: HEAD, OPTIONS, POST`" in PAGE
    assert "<code>GET /email</code>:</span>\n<code>405 Method Not Allowed</code> with " \
           "<code>Allow: HEAD, OPTIONS, POST</code>" in HOME


def test_on_post_the_definer_selects_the_action(sent):
    # /email maps one POST action, SEND; another POST verb selects no tool.
    assert answer(sent["a verb /email doesn't map"]) == (400, "unknown_action")


def test_an_id_is_an_argument_not_part_of_the_path(sent):
    assert answer(sent["an id in the path"]) == (404, "tool_not_found")
    assert "such as `/note/welcome`,\ngets `404 tool_not_found`" in PAGE


def test_the_home_page_shows_the_walkthroughs_delete():
    # The specimen and the comparison show the level-0 delete that test_examples.py replays (200).
    text = re.sub(r"<[^>]+>", "", HOME)
    assert text.count("DELETE /note?id=welcome") == 2 and "→ 200 OK" in text
    assert "DELETE /note?id=welcome\nHost: notes.localhost\n\n→ 200 OK" in (
        DOCS / "guide" / "walkthrough.md").read_text()


def test_the_dial_shows_requests_valid_under_the_demo_key():
    # Each level's nonce and guard come from a real send at that level (see the page's footnote);
    # the nonces are spent, but the tags must still verify under the walkthrough's demo key.
    body = json.dumps(NOTE, separators=(",", ":")).encode()
    shown = {}
    for start, end, name, value in re.findall(
            r'<span class="ws-shim" data-from="(\d)"(?: data-to="(\d)")?[^>]*>([\w-]+): <b>([^<]+)</b>', HOME):
        for level in range(int(start), int(end or 4) + 1):
            shown.setdefault(level, {})[name] = value
    assert sorted(shown) == [1, 2, 3, 4]
    for level, headers in shown.items():
        key = headers.get("Idempotency-Key", "")
        assert bool(key) == (level >= 2)
        assert guard_tag(KEY, "POST", "notes.localhost", "/email", headers["WebSpec-Nonce"], body, "",
                         "SEND", key) == headers["WebSpec-Guard"], level
        assert ("WebSpec-Clearance" in headers) == (level >= 3)
        assert ("WebSpec-Approval" in headers) == (level == 4)
        if level >= 3:
            ts = int(headers["WebSpec-Clearance"].split(":")[1])
            assert clearance(KEY, "notes", "POST", "send_email", NOTE, ts) == headers["WebSpec-Clearance"]


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
    # A DELETE with ARCHIVE, a DELETE verb, reaches the delete, which deletes the note outright.
    response = sent["DELETE with ARCHIVE"]
    assert answer(response) == (200, None) and response.json()["result"] == "deleted"
    assert sent["read after the DELETE"].json()["result"] == ""


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


@pytest.mark.parametrize("page", [HOME, PAGE], ids=["home", "how it works"])
def test_the_page_quotes_only_answers_seen_here(sent, page):
    seen = {f"{status} {error}" for status, error in map(answer, sent.values()) if error}
    quoted = set(re.findall(r"(?:`|<code>)([1-5]\d\d [a-z_]+)(?:`|</code>)", page))
    assert quoted, "the page quotes no answers"
    assert quoted <= seen, f"quoted on the page but not seen here: {sorted(quoted - seen)}"
