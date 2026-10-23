# Security policy

## Reporting a vulnerability

Report security issues privately through GitHub: on this repository's **Security** tab, choose
**Report a vulnerability**. Please don't open a public issue, pull request or discussion for a
suspected vulnerability.

A useful report says which commit you ran and how (a local run, the Linux service, the macOS
LaunchDaemon or the Docker stack), the requests or steps that show the problem, and what you
expected the gateway to do instead.

## Scope

- The reference gateway in `gateway/`, its deployments (`gateway/deploy/`, `docker/`), and the
  shim and demo server in `gateway/examples/`.
- The services in `services/` and the plugins in `plugins/`.
- The specification in `docs/spec/`: a rule that, followed as written, admits a request it should
  refuse.

The gateway's known gaps are listed under
[Known limits](https://i-m-a-g-i-n-e.github.io/WebSpec/spec/status/#known-limits). They don't
need a report, though a way to exploit one that the page doesn't describe does.

There are no releases yet, so reports apply to the latest commit on `main`.
