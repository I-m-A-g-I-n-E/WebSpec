"""WebSpec v2 route rule: one function per tool, and a destination's route map.

The interface these tests (and test_v2_http.py) are written against. Keep it this small.

webspec.routes
--------------

``route(tool_name: str, contract: ToolContract) -> Entry``
    The route rule for one tool, reading only its name and its (pinned, overridden) contract:
    split the name at its first ``_`` into ``first`` and ``rest``; if ``first`` is in the closed
    table and ``rest`` is not empty, the candidate is (table method, ``/{rest}`` verbatim, the
    table's definer on POST), else (POST, ``/{tool_name}``, INVOKE). The candidate stands if
    MB-1 admits its method for the contract and it is not PUT or DELETE for a non-idempotent
    tool. Otherwise the entry is ``/{tool_name}`` with the contract's fallback method:
    read-only -> GET, destructive and idempotent -> PUT, anything else -> POST + INVOKE.

    get read list search -> GET | set replace -> PUT | update modify patch append rename -> PATCH
    delete remove -> DELETE | create add -> POST CREATE | send -> POST SEND
    upload -> POST UPLOAD | trigger -> POST TRIGGER

``Entry`` (frozen dataclass)
    ``method``   "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
    ``path``     one segment with a leading slash, e.g. "/note"
    ``definer``  on POST the action verb ("CREATE", "SEND", "UPLOAD", "TRIGGER", "INVOKE");
                 "" on every other method
    ``fallback`` True only when the candidate was not admissible and the fallback was taken

``RouteMap`` (one destination's map; immutable, so swapping in a rebuilt map is atomic)
    ``RouteMap.build(destination: str, tools: Mapping[str, ToolContract]) -> RouteMap``
        The first build, at the destination's first tool listing. Entries that share a key
        (method, path, and on POST the definer) all move to ``/{tool_name}``, keeping method and
        definer; an entry already at its full name stays. Repeat until no key is shared. Every
        key is bound and its binding pinned.
    ``.moved -> frozenset[str]``
        The tools the first build moved. They stay at their full names in every rebuild.
    ``.rebuild(tools: Mapping[str, ToolContract], bindings: Mapping[str, str] | None = None) -> RouteMap``
        A rebuild, at every re-list and every config reload, carrying the binding pins forward.
        Each tool gets ``route()``'s entry, except that moved tools stay at ``/{tool_name}``. A key
        shared by two or more tools is closed (503 route_collision); nothing is moved. A pinned key
        that resolves to another tool is closed (503 binding_changed) unless ``bindings`` (the
        destination's config ``bindings``) maps the key string to exactly that tool, which re-pins
        the key. A pinned key that resolves to no tool is simply unmapped. A new, never-pinned,
        uncontested key is bound and pinned.
    ``.events -> tuple[BindingEvent, ...]``
        What this rebuild detected or confirmed, for the audit log.
    ``.resolve(method: str, path: str, definer: str = "") -> str | Refusal``
        The tool name, or why not. ``path`` is the request path ("/note"); ``definer`` is the
        canonical (upper-case) POST verb, already validated, or "" on other methods.
    ``.allow(path: str) -> str | None``
        The path's Allow value: the methods mapped on it (closed keys still count), plus HEAD and
        OPTIONS, in the order HEAD, OPTIONS, GET, POST, PUT, PATCH, DELETE; None if it maps nothing.
    ``.routes() -> dict[str, str]``
        Every open key as a key string mapped to its tool: "GET /note", "POST /email SEND". The key
        string is "METHOD /path", plus " DEFINER" on POST, the format of the config ``bindings``.

``Refusal`` (frozen dataclass): ``status`` 400 | 404 | 405 | 503; ``error`` "unknown_action" (a POST
    verb that maps no tool on a path that maps POST) | "tool_not_found" | "method_not_allowed" |
    "route_collision" | "binding_changed"; ``allow`` the path's Allow value on a 405, else None.

``BindingEvent`` (frozen dataclass): ``kind`` "binding_changed" | "binding_confirmed";
    ``binding`` the key string; ``pinned_tool`` the tool the key was pinned to; ``tool`` the tool
    it now resolves to.

In the gateway (test_v2_http.py)
--------------------------------

- ``webspec.handlers.route_maps: dict[str, RouteMap]``, one map per destination for the
  process lifetime, next to ``handlers.contract_pins``. Binding pins live in the maps, so they
  survive a destination being removed from the config and added back, as contract pins do.
  Tests replace it with ``{}`` for a fresh process.
- ``ServiceEntry.bindings: dict[str, str]``, parsed from the destination's config ``bindings``
  (``{"POST /email CREATE": "add_email"}``) and read by every rebuild.
- The first build logs one line (INFO or above, a ``webspec`` logger) naming the moved tools.
- Audit (the spec draft's AU-5): a detected change writes a record with ``outcome``
  "binding_changed" and a confirmation one with ``outcome`` "binding_confirmed". Its ``method``,
  ``path`` and, for a POST key, ``definer`` give the key; ``tool`` is the tool the key now
  resolves to and ``pinned_tool`` the tool it was pinned to.
- Refusals: ``404 tool_not_found``, ``405 method_not_allowed`` with the path's ``Allow`` (also for
  methods such as TRACE), ``400 unknown_action`` (its body points to ``OPTIONS /{noun}``),
  ``503 route_collision``, ``503 binding_changed``, ``400 missing_definer`` (POST only),
  ``400 unknown_definer`` (an empty value, a ":", a comma, two field lines or an unknown word),
  ``400 definer_family_mismatch``. Every refusal and every discovery answer carries
  ``Cache-Control: no-store``.
- Discovery bodies: ``OPTIONS /`` and ``OPTIONS /{noun}`` answer ``{"entries": [...]}``, one
  entry per mapped method (per definer on POST), each ``{"path", "method", "definer" (POST),
  "tool", "inputSchema", "contract", "requirements"}``; ``OPTIONS /`` lists every path's
  entries (the whole map). ``GET /`` answers ``{"tools": [...]}`` and each tool carries its
  entry as ``"method"``, ``"path"`` and on POST ``"definer"``. ``requirements`` has one boolean
  per RQ-1 row: guard, definer (true on POST only), empty_body, idempotency_key, clearance,
  approval; no bookend.
- Header names: WebSpec-Definer, -Guard, -Nonce, -Approval, -Clearance, -Provenance (request);
  WebSpec-Level, -Tier, -Taint, -Status, -Tool-Count, -Service (response); Idempotency-Key and
  Idempotent-Replayed kept; nothing named X-*.
"""

from __future__ import annotations

import itertools

import pytest
from mcp.types import ToolAnnotations

from webspec.methods import ToolContract, admissible_methods, contract_from_annotations

POST_FAMILY = {"CREATE", "SEND", "INVOKE", "TRIGGER", "UPLOAD"}

try:
    from webspec import routes
except ImportError as exc:  # before v2: each test below fails, naming what is missing
    class _Missing:
        def __getattr__(self, name, _why=f"{type(exc).__name__}: {exc}"):
            if name.startswith("__"):  # pytest's collector probes these
                raise AttributeError(name)
            pytest.fail(f"webspec.routes.{name} is missing, so the v2 route rule is not implemented ({_why})")

    routes = _Missing()


# ── Contracts, as the gateway derives them from MCP annotations (TC-1 to TC-3) ──

READ_ONLY = contract_from_annotations(ToolAnnotations(readOnlyHint=True, openWorldHint=False))
ADDITIVE = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=False)
ADDITIVE_IDEMPOTENT = ToolContract(read_only=False, destructive=False, idempotent=True, open_world=False)
DESTRUCTIVE = ToolContract(read_only=False, destructive=True, idempotent=False, open_world=False)
DESTRUCTIVE_IDEMPOTENT = ToolContract(read_only=False, destructive=True, idempotent=True, open_world=False)
UNANNOTATED = contract_from_annotations(None)  # TC-2: strict, destructive and not idempotent
ALL_CONTRACTS = {
    "read-only": READ_ONLY, "additive": ADDITIVE, "additive idempotent": ADDITIVE_IDEMPOTENT,
    "destructive": DESTRUCTIVE, "destructive idempotent": DESTRUCTIVE_IDEMPOTENT, "unannotated": UNANNOTATED,
}


def entry_of(tool_name: str, contract: ToolContract) -> tuple[str, str, str, bool]:
    e = routes.route(tool_name, contract)
    return e.method, e.path, e.definer, e.fallback


def refusal(outcome) -> tuple[int, str]:
    assert not isinstance(outcome, str), f"resolved to {outcome!r}, expected a refusal"
    return outcome.status, outcome.error


def test_the_contracts_used_here_are_what_the_gateway_derives():
    assert READ_ONLY.read_only and not READ_ONLY.destructive
    assert UNANNOTATED.destructive and not UNANNOTATED.idempotent and not UNANNOTATED.read_only


# ── route(): the closed table, one row at a time ──

TABLE = {
    "get": ("GET", ""), "read": ("GET", ""), "list": ("GET", ""), "search": ("GET", ""),
    "set": ("PUT", ""), "replace": ("PUT", ""),
    "update": ("PATCH", ""), "modify": ("PATCH", ""), "patch": ("PATCH", ""), "append": ("PATCH", ""),
    "rename": ("PATCH", ""),
    "delete": ("DELETE", ""), "remove": ("DELETE", ""),
    "create": ("POST", "CREATE"), "add": ("POST", "CREATE"),
    "send": ("POST", "SEND"), "upload": ("POST", "UPLOAD"), "trigger": ("POST", "TRIGGER"),
}
# A contract under which each table method is admissible and allowed (PUT/DELETE need idempotent).
FITS = {"GET": READ_ONLY, "PUT": ADDITIVE_IDEMPOTENT, "PATCH": ADDITIVE, "DELETE": DESTRUCTIVE_IDEMPOTENT,
        "POST": ADDITIVE}


@pytest.mark.parametrize("first", list(TABLE))
def test_each_table_row(first):
    method, definer = TABLE[first]
    assert entry_of(f"{first}_widget", FITS[method]) == (method, "/widget", definer, False)


def test_the_table_is_closed_no_synonyms():
    for name in ("fetch_widget", "put_widget", "post_widget", "edit_widget", "destroy_widget", "invoke_widget",
                 "query_widget", "make_widget", "browser_click", "lean_build", "corpus_create"):
        assert entry_of(name, ADDITIVE) == ("POST", f"/{name}", "INVOKE", False), name


def test_the_rest_is_kept_verbatim_never_pluralized():
    assert entry_of("list_note", READ_ONLY)[:3] == ("GET", "/note", "")
    assert entry_of("list_pull_requests", READ_ONLY)[:3] == ("GET", "/pull_requests", "")
    assert entry_of("delete_note_permanently", DESTRUCTIVE_IDEMPOTENT)[:3] == ("DELETE", "/note_permanently", "")
    assert entry_of("get_Note", READ_ONLY)[:3] == ("GET", "/Note", "")
    assert entry_of("send_email_now", ADDITIVE)[:3] == ("POST", "/email_now", "SEND")


def test_the_name_splits_at_its_first_underscore():
    # first = "get", rest = "and_delete_x": the GET candidate, not a scan for "delete".
    assert entry_of("get_and_delete_x", READ_ONLY) == ("GET", "/and_delete_x", "", False)


def test_a_name_without_an_underscore_takes_its_full_name():
    assert entry_of("run", ADDITIVE) == ("POST", "/run", "INVOKE", False)
    assert entry_of("read", READ_ONLY) == ("GET", "/read", "", True)  # op-auth's read: POST is not admissible
    assert entry_of("delete", DESTRUCTIVE_IDEMPOTENT) == ("PUT", "/delete", "", True)


def test_an_empty_rest_takes_the_full_name():
    assert entry_of("get_", ADDITIVE) == ("POST", "/get_", "INVOKE", False)
    assert entry_of("get_", READ_ONLY) == ("GET", "/get_", "", True)
    assert entry_of("send_", ADDITIVE) == ("POST", "/send_", "INVOKE", False)


# ── route(): admissibility and the fallback ──

@pytest.mark.parametrize("tool_name,contract,expected", [
    # the contract's own examples
    ("delete_note", DESTRUCTIVE_IDEMPOTENT, ("DELETE", "/note", "", False)),
    ("read_note", READ_ONLY, ("GET", "/note", "", False)),
    ("send_email", ADDITIVE, ("POST", "/email", "SEND", False)),
    ("run_query", ADDITIVE, ("POST", "/run_query", "INVOKE", False)),
    ("get_weather", UNANNOTATED, ("POST", "/get_weather", "INVOKE", True)),
    ("get_and_delete_x", DESTRUCTIVE, ("POST", "/get_and_delete_x", "INVOKE", True)),
    ("read", READ_ONLY, ("GET", "/read", "", True)),
], ids=["delete_note", "read_note", "send_email", "run_query", "unannotated get_weather",
        "destructive get_and_delete_x", "op-auth read"])
def test_the_contract_examples(tool_name, contract, expected):
    assert entry_of(tool_name, contract) == expected


@pytest.mark.parametrize("tool_name,contract,expected", [
    # read-only -> GET
    ("create_report", READ_ONLY, ("GET", "/create_report", "", True)),
    ("run_query", READ_ONLY, ("GET", "/run_query", "", True)),
    ("delete_cache", READ_ONLY, ("GET", "/delete_cache", "", True)),
    # destructive and idempotent -> PUT
    ("update_widget", DESTRUCTIVE_IDEMPOTENT, ("PUT", "/update_widget", "", True)),
    ("create_widget", DESTRUCTIVE_IDEMPOTENT, ("PUT", "/create_widget", "", True)),
    ("get_widget", DESTRUCTIVE_IDEMPOTENT, ("PUT", "/get_widget", "", True)),
    ("run_query", DESTRUCTIVE_IDEMPOTENT, ("PUT", "/run_query", "", True)),
    # anything else -> POST + INVOKE
    ("get_widget", ADDITIVE, ("POST", "/get_widget", "INVOKE", True)),
    ("list_widgets", UNANNOTATED, ("POST", "/list_widgets", "INVOKE", True)),
    ("update_widget", UNANNOTATED, ("POST", "/update_widget", "INVOKE", True)),
    ("delete_widget", ADDITIVE, ("POST", "/delete_widget", "INVOKE", True)),
    ("set_widget", DESTRUCTIVE, ("POST", "/set_widget", "INVOKE", True)),
])
def test_an_inadmissible_candidate_takes_the_contracts_fallback(tool_name, contract, expected):
    assert entry_of(tool_name, contract) == expected


@pytest.mark.parametrize("tool_name,contract", [
    ("delete_draft", UNANNOTATED),        # MB-1 admits DELETE for it, but it is not idempotent
    ("delete_draft", DESTRUCTIVE),
    ("remove_member", DESTRUCTIVE),
    ("set_flag", ADDITIVE),               # not idempotent: MB-1 admits POST and PATCH only
    ("replace_flag", ADDITIVE),
    ("set_flag", DESTRUCTIVE),
])
def test_a_non_idempotent_tool_is_never_routed_to_put_or_delete(tool_name, contract):
    assert entry_of(tool_name, contract) == ("POST", f"/{tool_name}", "INVOKE", True)


def test_destructive_and_non_idempotent_creates_and_sends_stay_on_post():
    assert entry_of("create_widget", DESTRUCTIVE) == ("POST", "/widget", "CREATE", False)
    assert entry_of("send_email", UNANNOTATED) == ("POST", "/email", "SEND", False)


def test_op_auth_tools():
    assert entry_of("read", READ_ONLY)[:3] == ("GET", "/read", "")
    assert entry_of("list_vaults", READ_ONLY)[:3] == ("GET", "/vaults", "")
    assert entry_of("list_items", READ_ONLY)[:3] == ("GET", "/items", "")
    assert entry_of("get_item", READ_ONLY)[:3] == ("GET", "/item", "")
    assert entry_of("run", ADDITIVE)[:3] == ("POST", "/run", "INVOKE")


def _fallback_method(contract: ToolContract) -> str:
    if contract.read_only:
        return "GET"
    if contract.destructive and contract.idempotent:
        return "PUT"
    return "POST"


NAMES = [f"{first}_widget" for first in TABLE] + [
    "widget", "get_", "run_query", "browser_click", "get_and_delete_x", "list_pull_requests", "x_y_z"]


@pytest.mark.parametrize("contract_name", list(ALL_CONTRACTS))
def test_every_entry_is_admissible_and_well_formed(contract_name):
    contract = ALL_CONTRACTS[contract_name]
    for name in NAMES:
        e = routes.route(name, contract)
        assert e.method in admissible_methods(contract), (name, e)  # the map only narrows MB-1
        if e.method in ("PUT", "DELETE"):
            assert contract.idempotent, (name, e)  # RFC 9110 §9.2.2
        if e.method == "GET":
            assert contract.read_only, (name, e)  # GET is never bound unless the contract is read-only
        assert e.path.startswith("/") and "/" not in e.path[1:] and len(e.path) > 1, (name, e)
        if e.method == "POST":
            assert e.definer in POST_FAMILY, (name, e)
        else:
            assert e.definer == "", (name, e)
        if e.fallback:
            assert (e.method, e.path) == (_fallback_method(contract), f"/{name}"), (name, e)
            assert e.definer == ("INVOKE" if e.method == "POST" else ""), (name, e)


def test_route_reads_only_the_name_and_the_contract():
    a = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=True, tier="dangerous")
    b = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=False, tier="open")
    for name in NAMES:
        assert routes.route(name, a) == routes.route(name, b) == routes.route(name, a)


# ── RouteMap: the first build ──

NOTES = {"read_note": READ_ONLY, "send_email": ADDITIVE, "delete_note": DESTRUCTIVE_IDEMPOTENT}
DOC = {"read_doc": READ_ONLY, "create_doc": ADDITIVE, "set_doc": ADDITIVE_IDEMPOTENT, "update_doc": ADDITIVE,
       "delete_doc": DESTRUCTIVE_IDEMPOTENT}


def test_build_gives_every_tool_its_entry():
    m = routes.RouteMap.build("notes", NOTES)
    assert m.routes() == {"GET /note": "read_note", "POST /email SEND": "send_email", "DELETE /note": "delete_note"}
    assert m.moved == frozenset()


def test_resolve():
    m = routes.RouteMap.build("notes", NOTES)
    assert m.resolve("GET", "/note") == "read_note"
    assert m.resolve("DELETE", "/note") == "delete_note"
    assert m.resolve("POST", "/email", "SEND") == "send_email"


def test_a_method_the_path_does_not_map_is_405_with_the_paths_allow():
    m = routes.RouteMap.build("notes", NOTES)
    for method, definer in (("PUT", ""), ("PATCH", ""), ("POST", "SEND")):
        outcome = m.resolve(method, "/note", definer)
        assert refusal(outcome) == (405, "method_not_allowed")
        assert outcome.allow == "HEAD, OPTIONS, GET, DELETE"
    outcome = m.resolve("GET", "/email")
    assert refusal(outcome) == (405, "method_not_allowed") and outcome.allow == "HEAD, OPTIONS, POST"


@pytest.mark.parametrize("method,path,definer", [
    ("GET", "/nothing", ""),
    ("GET", "/read_note", ""),            # the full name is not a route any more
    ("DELETE", "/delete_note", ""),
    ("POST", "/send_email", "SEND"),
    ("POST", "/send/email", "SEND"),      # PA-1's slash fold is gone
    ("GET", "/note/welcome", ""),         # one segment only; ids stay in the query
])
def test_what_maps_nothing_is_404(method, path, definer):
    m = routes.RouteMap.build("notes", NOTES)
    assert refusal(m.resolve(method, path, definer)) == (404, "tool_not_found")


@pytest.mark.parametrize("definer", ["TRIGGER", "CREATE"])
def test_a_post_action_the_path_does_not_map_is_400_unknown_action(definer):
    m = routes.RouteMap.build("notes", NOTES)
    assert refusal(m.resolve("POST", "/email", definer)) == (400, "unknown_action")


def test_allow_is_per_path_in_canonical_order():
    m = routes.RouteMap.build("docs", {**DOC, **NOTES})
    assert m.allow("/doc") == "HEAD, OPTIONS, GET, POST, PUT, PATCH, DELETE"
    assert m.allow("/note") == "HEAD, OPTIONS, GET, DELETE"
    assert m.allow("/email") == "HEAD, OPTIONS, POST"
    assert m.allow("/nothing") is None
    assert m.allow("/read_doc") is None


def test_the_definer_selects_the_post_action_on_a_path():
    m = routes.RouteMap.build("mail", {"send_email": ADDITIVE, "create_email": ADDITIVE, "upload_email": ADDITIVE})
    assert m.routes() == {"POST /email SEND": "send_email", "POST /email CREATE": "create_email",
                          "POST /email UPLOAD": "upload_email"}
    assert m.resolve("POST", "/email", "CREATE") == "create_email"
    assert m.resolve("POST", "/email", "UPLOAD") == "upload_email"
    assert m.allow("/email") == "HEAD, OPTIONS, POST"


def test_the_first_build_moves_every_collider_to_its_full_name():
    m = routes.RouteMap.build("tracker", {"list_issues": READ_ONLY, "search_issues": READ_ONLY,
                                          "create_email": ADDITIVE, "add_email": ADDITIVE, "send_email": ADDITIVE})
    assert m.routes() == {
        "GET /list_issues": "list_issues", "GET /search_issues": "search_issues",
        # moved, keeping method and definer
        "POST /create_email CREATE": "create_email", "POST /add_email CREATE": "add_email",
        "POST /email SEND": "send_email",
    }
    assert m.moved == frozenset({"list_issues", "search_issues", "create_email", "add_email"})
    assert refusal(m.resolve("GET", "/issues")) == (404, "tool_not_found")
    assert refusal(m.resolve("POST", "/email", "CREATE")) == (400, "unknown_action")


def test_an_entry_already_at_its_full_name_never_moves():
    m = routes.RouteMap.build("w", {"widget": READ_ONLY, "get_widget": READ_ONLY})
    assert m.routes() == {"GET /widget": "widget", "GET /get_widget": "get_widget"}
    assert m.moved == frozenset({"get_widget"})
    m = routes.RouteMap.build("w", {"widget": DESTRUCTIVE_IDEMPOTENT, "set_widget": DESTRUCTIVE_IDEMPOTENT})
    assert m.routes() == {"PUT /widget": "widget", "PUT /set_widget": "set_widget"}


def test_the_first_build_repeats_until_no_key_is_shared():
    # read_x and get_x share GET /x and move; read_x's new key is read_read_x's entry, so that one moves too.
    m = routes.RouteMap.build("w", {"read_x": READ_ONLY, "get_x": READ_ONLY, "read_read_x": READ_ONLY})
    assert m.routes() == {"GET /read_x": "read_x", "GET /get_x": "get_x", "GET /read_read_x": "read_read_x"}
    assert m.moved == frozenset({"read_x", "get_x", "read_read_x"})


def test_keys_differ_by_method_and_by_post_definer():
    m = routes.RouteMap.build("w", {"get_x": READ_ONLY, "x": ADDITIVE, "send_x": ADDITIVE, "create_x": ADDITIVE})
    assert m.routes() == {"GET /x": "get_x", "POST /x INVOKE": "x", "POST /x SEND": "send_x",
                          "POST /x CREATE": "create_x"}
    assert m.moved == frozenset()


# ── RouteMap: rebuilds and binding pins ──

def test_a_rebuild_with_the_same_tools_changes_nothing():
    first = routes.RouteMap.build("notes", NOTES)
    again = first.rebuild(NOTES)
    assert again.routes() == first.routes() and again.events == ()


def test_a_rebuild_keeps_the_first_builds_moves():
    first = routes.RouteMap.build("tracker", {"list_issues": READ_ONLY, "search_issues": READ_ONLY})
    again = first.rebuild({"list_issues": READ_ONLY, "search_issues": READ_ONLY})
    assert again.routes() == {"GET /list_issues": "list_issues", "GET /search_issues": "search_issues"}
    assert again.resolve("GET", "/list_issues") == "list_issues"
    assert refusal(again.resolve("GET", "/issues")) == (404, "tool_not_found")


def test_a_collision_that_first_appears_in_a_rebuild_closes_the_key():
    first = routes.RouteMap.build("tracker", {"list_issues": READ_ONLY})
    assert first.resolve("GET", "/issues") == "list_issues"
    m = first.rebuild({"list_issues": READ_ONLY, "search_issues": READ_ONLY})
    assert refusal(m.resolve("GET", "/issues")) == (503, "route_collision")
    assert m.allow("/issues") == "HEAD, OPTIONS, GET"  # a closed key still counts as mapped
    # A rebuild never moves anyone, and never picks one.
    assert refusal(m.resolve("GET", "/list_issues")) == (404, "tool_not_found")
    assert refusal(m.resolve("GET", "/search_issues")) == (404, "tool_not_found")
    # The key reopens once a rebuild finds it unshared again.
    assert m.rebuild({"list_issues": READ_ONLY}).resolve("GET", "/issues") == "list_issues"


def test_a_same_key_rename_is_refused_until_the_operator_confirms_it():
    looser = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=False, tier="open")
    stricter = ToolContract(read_only=False, destructive=False, idempotent=False, open_world=True, tier="dangerous")
    first = routes.RouteMap.build("mail", {"create_email": stricter})
    assert first.resolve("POST", "/email", "CREATE") == "create_email"

    renamed = first.rebuild({"add_email": looser})
    assert refusal(renamed.resolve("POST", "/email", "CREATE")) == (503, "binding_changed")
    assert [(e.kind, e.binding, e.pinned_tool, e.tool) for e in renamed.events] == [
        ("binding_changed", "POST /email CREATE", "create_email", "add_email")]

    # A confirmation that names another tool confirms nothing.
    assert refusal(renamed.rebuild({"add_email": looser}, {"POST /email CREATE": "other_tool"})
                   .resolve("POST", "/email", "CREATE")) == (503, "binding_changed")

    confirmed = renamed.rebuild({"add_email": looser}, {"POST /email CREATE": "add_email"})
    assert confirmed.resolve("POST", "/email", "CREATE") == "add_email"
    assert [(e.binding, e.pinned_tool, e.tool) for e in confirmed.events if e.kind == "binding_confirmed"] == [
        ("POST /email CREATE", "create_email", "add_email")]
    # Re-pinned: later rebuilds serve it with nothing more to confirm.
    later = confirmed.rebuild({"add_email": looser}, {"POST /email CREATE": "add_email"})
    assert later.resolve("POST", "/email", "CREATE") == "add_email" and later.events == ()


def test_a_rename_onto_another_key_leaves_the_old_key_unmapped():
    first = routes.RouteMap.build("mail", {"send_email": ADDITIVE, "read_email": READ_ONLY})
    m = first.rebuild({"create_email": ADDITIVE, "read_email": READ_ONLY})
    assert refusal(m.resolve("POST", "/email", "SEND")) == (400, "unknown_action")
    # The new key was never pinned and nobody shares it: bound and pinned.
    assert m.resolve("POST", "/email", "CREATE") == "create_email"
    assert m.events == ()


def test_a_pinned_key_whose_tool_is_gone_gets_the_new_maps_refusal():
    first = routes.RouteMap.build("notes", NOTES)
    m = first.rebuild({"read_note": READ_ONLY, "send_email": ADDITIVE})
    outcome = m.resolve("DELETE", "/note")
    assert refusal(outcome) == (405, "method_not_allowed") and outcome.allow == "HEAD, OPTIONS, GET"
    assert refusal(m.rebuild({}).resolve("GET", "/note")) == (404, "tool_not_found")


def test_a_pin_outlives_its_tool_so_a_later_claimant_is_refused():
    first = routes.RouteMap.build("mail", {"create_email": ADDITIVE})
    gone = first.rebuild({})
    assert refusal(gone.resolve("POST", "/email", "CREATE")) == (404, "tool_not_found")
    back = gone.rebuild({"add_email": ADDITIVE})
    assert refusal(back.resolve("POST", "/email", "CREATE")) == (503, "binding_changed")


def test_a_confirmation_only_confirms_it_never_routes():
    m = routes.RouteMap.build("mail", {"send_email": ADDITIVE}).rebuild(
        {"send_email": ADDITIVE}, {"POST /email CREATE": "send_email"})
    assert m.routes() == {"POST /email SEND": "send_email"}
    assert refusal(m.resolve("POST", "/email", "CREATE")) == (400, "unknown_action")


def test_a_tightened_contract_moves_the_tool_off_get():
    first = routes.RouteMap.build("docs", DOC)
    assert first.resolve("GET", "/doc") == "read_doc"
    m = first.rebuild({**DOC, "read_doc": DESTRUCTIVE})
    assert m.resolve("POST", "/read_doc", "INVOKE") == "read_doc"
    outcome = m.resolve("GET", "/doc")
    assert refusal(outcome) == (405, "method_not_allowed")
    assert outcome.allow == "HEAD, OPTIONS, POST, PUT, PATCH, DELETE"


def test_rebuild_returns_a_new_map_and_leaves_the_old_one_untouched():
    first = routes.RouteMap.build("notes", NOTES)
    before = first.routes()
    first.rebuild({"read_note": READ_ONLY})
    assert first.routes() == before and first.resolve("DELETE", "/note") == "delete_note"


def test_combinations_of_table_words_and_contracts_never_raise():
    for first, contract in itertools.product(list(TABLE) + ["run", "x"], ALL_CONTRACTS.values()):
        routes.RouteMap.build("w", {f"{first}_a": contract, f"{first}_b": contract, f"{first}": contract})
