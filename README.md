# WebSpec

**Every AI tool call as an ordinary HTTP request that says what it does, so the web and a
gateway in front of your MCP servers can enforce it.**

```http
POST /email
Host: notes.localhost
Content-Type: application/json
WebSpec-Definer: SEND

{"id":"welcome","to":"ana@example.com"}
```

The host names the destination, here the MCP server `notes`. The path names what the call acts
on, and the method says what kind of act it is. `POST` covers many kinds, so on `POST` the
definer names the action. The body carries the arguments. The gateway learns each tool's
contract from its MCP annotations or from the operator, never from the request, and maps each
tool to one path and to one method that the contract admits. It refuses any other method on
that path, a `POST` without a definer, and a change whose definer belongs to another method.
A *security level* from 0 to 4 decides how much proof each call must carry. At level 4, a
person signs the method, destination, path, arguments, and body before the gateway calls a
destructive tool, a `dangerous` one, or one that changes something in outside systems. The MCP
servers stay unchanged; the gateway sits in front of them.

**Read it:** <https://i-m-a-g-i-n-e.github.io/WebSpec/>. Start with the home page and
[How it works](https://i-m-a-g-i-n-e.github.io/WebSpec/guide/how-it-works/). The **Spec** pages are
normative (except Status and roadmap); the **Guide** pages have the quickstart, walkthroughs,
and rationale.

## What's here

| Path | What it is |
|---|---|
| `docs/` | The published site: `spec/` (normative) and `guide/` (how-to) |
| `gateway/` | The reference gateway (Starlette): route map, method binding, levels 0–4, audit chain |
| `gateway/deploy/` | Production deployments: a Linux system service and a macOS LaunchDaemon, each running the gateway as its own user |
| `gateway/tools/setup-caddy.sh` | Caddy in front of the Linux gateway: loopback listeners held by systemd, filtered logs |
| `gateway/examples/` | A demo MCP server, a reference harness shim, and the walkthrough generator |
| `gateway/webspec_registry/` | Tool registry and resolver (experimental) |
| `services/` | MCP servers used in the reference deployment (`mail-proton`, `op-auth`) |
| `plugins/` | Claude Code plugins (`webspector`, `webspec-red-team`, `protonmail`) |
| `docker/` | A hardened compose stack: the gateway behind Caddy |
| `gateway/systemd/`, `gateway/launchd/` | Development units that run the gateway as your login user; don't expose them |
| `design/` | Design notes and implementation plans (not published) |

## Try it

```bash
git clone https://github.com/I-m-A-g-I-n-E/WebSpec && cd WebSpec
python -m venv .venv && source .venv/bin/activate
pip install ./gateway
export WEBSPEC_GUARD_KEY=$(openssl rand -hex 32)
echo '{"mcpServers": {"notes": {"command": "python", "args": ["gateway/examples/demo_server.py"]}}}' > webspec.json
WEBSPEC_CONFIG=$PWD/webspec.json python -m webspec &
sleep 2
curl -s "http://notes.localhost:7001/note?id=welcome"
curl -si "http://notes.localhost:7001/email?id=welcome&to=eve@example.com"
```

The demo server's `read_note` is served as `GET /note`, so the first call answers
`{"result":"Hello from WebSpec."}`. `send_email` isn't read-only, so `GET` can't reach it, and
the second call is refused (headers abridged):

```text
HTTP/1.1 405 Method Not Allowed
allow: HEAD, OPTIONS, POST
cache-control: no-store

{"error":"method_not_allowed","detail":"/email does not map GET; it allows HEAD, OPTIONS, POST"}
```

Then follow the [quickstart](https://i-m-a-g-i-n-e.github.io/WebSpec/guide/). To run the gateway
for real, as its own user behind Caddy, see
[Deploying](https://i-m-a-g-i-n-e.github.io/WebSpec/guide/deploy/).

## Develop

```bash
pip install ./gateway pytest pyyaml -r requirements.txt
(cd gateway && python -m pytest -q)           # gateway, registry, and walkthrough tests
python -m pytest -q docs/tests                # rule IDs, rendering, nav, code-to-docs links
mkdocs build --strict                         # the docs build CI runs
mkdocs serve                                  # preview the site at http://localhost:8000
```

The site deploys from `main` through GitHub Actions (`.github/workflows/deploy-docs.yml`).

## Authors

- Preston Richey
- Claude (Anthropic)

## License

TBD
