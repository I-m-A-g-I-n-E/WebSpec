# WebSpec

**Every AI tool call as an ordinary HTTP request that says exactly what it does, so the web and
a gateway in front of your MCP servers can enforce it.**

```http
POST /send_email HTTP/1.1
Host: mail.example.com
X-Gimme-Definer: SEND

{"to": "ops@example.com", "subject": "Deploy finished"}
```

Each part of the call sits in its own slot. The host says who handles it, the method what
kind of act it is, the path which act, the definer says the verb aloud, and the body says
with what. The gateway refuses any request whose parts disagree. A *security level* from 0
to 4 decides how much proof each call must carry. At the top, a person signs the exact
request before anything destructive, dangerous, or open-world and mutating happens. WebSpec complements
MCP: it is the HTTP face and the firewall in front of unchanged MCP servers.

**Read the spec:** <https://i-m-a-g-i-n-e.github.io/WebSpec/>. The **Spec** tab is normative;
the **Guide** tab has the quickstart, walkthroughs, and rationale.

## What's here

| Path | What it is |
|---|---|
| `docs/` | The published site: `spec/` (normative) and `guide/` (how-to) |
| `gateway/` | The reference gateway (Starlette): method binding, levels 0–4, audit chain |
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
python -m venv .venv && source .venv/bin/activate
pip install ./gateway
export WEBSPEC_GUARD_KEY=$(openssl rand -hex 32)
echo '{"mcpServers": {"notes": {"command": "python", "args": ["gateway/examples/demo_server.py"]}}}' > webspec.json
WEBSPEC_CONFIG=$PWD/webspec.json python -m webspec &
sleep 2
curl -s "http://notes.localhost:7001/read_note?id=welcome"
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
