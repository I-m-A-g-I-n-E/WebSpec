# WebSpec gateway image. docker/docker-compose.yml builds it with the repo root as context.
#
# Hardening (docs/spec/audit-deployment.md):
#   DP-1  The gateway runs as the dedicated user `webspec` (uid/gid 10001, no login shell),
#         never as root.
#   DP-2  PID 1 is the image's own init (docker/init.py), not Docker's docker-init. It
#         starts the entrypoint, forwards signals to the gateway, continues it when a stdio
#         MCP server stops it, reaps the processes stdio servers leave behind, and makes
#         itself non-dumpable before it starts anything, so the stdio servers cannot read the
#         container's environment from /proc/1/environ. The gateway makes itself non-dumpable
#         too. Under another PID 1 whose environment its user can read (`docker run --init`,
#         a daemon-wide "init": true), the init refuses to start: run the image with
#         `--init=false`, as compose's `init: false` does.
#   DP-4  The code (/app, the init, and the installed package under /usr/local) stays
#         root-owned, so the runtime user cannot change it. State, the audit log, lives in
#         the user's HOME, /var/lib/webspec (owned by `webspec`, mode 0700), which the
#         compose stack mounts as the `webspec-state` volume.
FROM python:3.11-slim

RUN groupadd --gid 10001 webspec \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/webspec \
        --shell /usr/sbin/nologin --no-log-init webspec \
 && install -d -o 10001 -g 10001 -m 0700 /var/lib/webspec

WORKDIR /app
COPY gateway/ /app/gateway/
RUN pip install --no-cache-dir ./gateway
COPY docker/init.py /usr/local/libexec/webspec-init.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0644 /usr/local/libexec/webspec-init.py && chmod 0755 /usr/local/bin/entrypoint.sh

# WEBSPEC_CONFIG defaults to a path outside the writable state directory, so the gateway
# never falls back to a config file its own user (or a stdio MCP server it spawns) can write.
#
# PYTHONNOUSERSITE: HOME is the state directory, which the stdio MCP servers can write
# (they run as uid 10001 too). Python would otherwise add the user site-packages directory
# under HOME (~/.local/lib/python3.11/site-packages) to sys.path and run the .pth files
# there at startup, so a stdio server could plant code that runs inside the gateway, guard
# key and all, at its next start. This also covers every other Python process started with
# the image's environment: `docker exec`, a replaced command, the audit-log containers in
# docker/README.md. The init and the gateway add -I (isolated mode) on top. The stdio
# servers do not inherit this environment; docker/README.md, "Bring your own MCP service",
# says what they get.
ENV HOME=/var/lib/webspec \
    WEBSPEC_AUDIT_LOG=/var/lib/webspec/gateway-audit.jsonl \
    WEBSPEC_CONFIG=/config/claude.json \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
# PID 1: the init runs the entrypoint, which sources the guard key and execs the command.
# --listen: the init binds the gateway's port before it starts anything, and holds it for the
# container's life. When the gateway exits, the init kills every other process before it lets
# the port go, though it waits only 2 seconds for a process of another user, which it cannot
# kill (a root `docker exec`). So no stdio server, nor anything one leaves behind, can take the
# port while the gateway shuts down, or after (DP-9).
ENTRYPOINT ["/usr/local/bin/python3", "-I", "/usr/local/libexec/webspec-init.py", "--listen", "/usr/local/bin/entrypoint.sh"]
# -I: no user site-packages, no PYTHON* variables, and no current directory on sys.path, so
# nothing outside the root-owned install is imported, whatever the environment says.
CMD ["python", "-I", "-m", "webspec"]
