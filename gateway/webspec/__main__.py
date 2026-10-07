"""Entry point: python -m webspec → uvicorn on loopback.

Under systemd or launchd the gateway serves on the listening socket the service manager
passes in (webspec/activation.py), which stays bound while the gateway restarts. The Docker
image's init (docker/init.py --listen) passes one the same way; it stays bound while the
gateway shuts down, and the container exits with the gateway. Otherwise the gateway binds
WEBSPEC_HOST (default 127.0.0.1) at WEBSPEC_INTERNAL_PORT (falling back to WEBSPEC_PORT,
default 7001) itself.

With WEBSPEC_REQUIRE_GUARD_KEY=1, which the Linux unit sets, the gateway does not start
without a usable key in WEBSPEC_GUARD_KEY itself (GD-5): it exits with STARTUP_FAILURE
before it serves.
"""

import asyncio
import logging
import os
import sys

import uvicorn

from . import activation
from .app import create_app
from .config import GuardKeyError, env_guard_key
from .hardening import harden_process
from .pool import DEFAULT_TIMEOUT

logger = logging.getLogger("webspec")

STARTUP_FAILURE = 3  # uvicorn's exit status when the server cannot start

# How long a shutdown waits for requests in flight before it cancels them: long enough for a
# tool call already running (pool.DEFAULT_TIMEOUT), then the process exits whatever it waits
# on. Without a limit, a request that never ends (a stdio server that sends the gateway SIGTERM
# and never answers its handshake) keeps the gateway in shutdown for good, answering nothing,
# and the service manager never restarts it (DP-7, F3). systemd and launchd stop the gateway
# with their own, longer limits.
GRACEFUL_SHUTDOWN_SECONDS = DEFAULT_TIMEOUT + 5

# uvicorn's stop closes at once every connection it is not reading a request on: one it has just
# accepted, and one that a client keeps alive between two requests, as Caddy does. A request
# already on its way over such a connection is lost: the kernel answers it with a reset, and
# Caddy answers a POST with 502. So a stop drains first. It stops accepting, which leaves new
# connections waiting for the next gateway in the socket that the service manager holds. For
# this long, it answers what arrives on the connections already open, like any call in flight
# (DP-7), and every answer says Connection: close, so that each client opens its next
# connection to the next gateway. Then uvicorn closes what is still idle.
ACCEPTED_GRACE_SECONDS = 0.25


def resolve_bind_host() -> str:
    """Default to loopback; require an explicit override to bind all interfaces (DP-8).

    An empty value counts as unset: uvicorn would read "" as every interface.
    """
    return (os.environ.get("WEBSPEC_HOST") or "").strip() or "127.0.0.1"


def guard_key_required() -> bool:
    """Whether WEBSPEC_REQUIRE_GUARD_KEY makes a usable WEBSPEC_GUARD_KEY a condition of starting.

    The Linux unit sets it to 1. It only ever tightens, so any value but an empty one or 0
    counts: a value meant to turn it on can never turn it off unnoticed.
    """
    return (os.environ.get("WEBSPEC_REQUIRE_GUARD_KEY") or "").strip() not in ("", "0")


class Draining:
    """ASGI wrapper: once ``on`` is set, every response says ``Connection: close``.

    uvicorn then closes the connection once the response is sent (h11 and httptools alike).
    """

    def __init__(self, app):
        self.app = app
        self.on = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_closing(message):
            if message["type"] == "http.response.start" and self.on:
                message = {**message, "headers": [*message.get("headers", ()), (b"connection", b"close")]}
            await send(message)

        await self.app(scope, receive, send_closing)


def graceful_server(app, **options) -> uvicorn.Server:
    """uvicorn's Server for ``app``, whose stop drains for ACCEPTED_GRACE_SECONDS first."""
    draining = Draining(app)

    class Server(uvicorn.Server):
        async def shutdown(self, sockets=None) -> None:
            for listener in self.servers:
                listener.close()
            for sock in sockets or []:
                sock.close()
            draining.on = True
            if self.server_state.connections:
                await asyncio.sleep(ACCEPTED_GRACE_SECONDS)
            await super().shutdown(sockets=sockets)  # closing them again does nothing

    return Server(uvicorn.Config(draining, **options))


def main() -> None:
    # WEBSPEC_INTERNAL_PORT is the gateway's listen port (behind Caddy)
    # Falls back to WEBSPEC_PORT for backward compatibility when running without Caddy
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", os.environ.get("WEBSPEC_PORT", "7001")))
    log_level = os.environ.get("WEBSPEC_LOG_LEVEL", "info").lower()

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if guard_key_required():
        # GD-5 where the deployment passes the key in WEBSPEC_GUARD_KEY: without a usable key
        # there, the start fails, so the service manager and the installer see it, instead of
        # a gateway that refuses every request that needs the key. config's own rule decides,
        # and nothing else is tried: not WEBSPEC_GUARD_KEY_FILE, not the ephemeral dev key.
        # First, so that the refusal is the one line the gateway logs.
        try:
            env_guard_key()
        except GuardKeyError as exc:  # names the variable, never its value
            logger.error("No usable guard key, not starting: %s. WEBSPEC_REQUIRE_GUARD_KEY is set, so the key "
                         "must be in WEBSPEC_GUARD_KEY itself, not in a key file or an ephemeral key (GD-5)", exc)
            sys.exit(STARTUP_FAILURE)
    harden_process()
    # Taken before anything else can start a child process, and never given up for a port of
    # our own: when activation was requested, a failure ends the process (DP-9).
    try:
        inherited = activation.inherited_socket()
    except activation.ActivationError as exc:
        logger.error("Socket activation failed, not starting: %s", exc)
        sys.exit(STARTUP_FAILURE)

    app = create_app()
    options = dict(
        log_level=log_level,
        # GET carries tool arguments in the URL; the audit log records them only as
        # hashes, so don't write them verbatim to an access log by default.
        access_log=os.environ.get("WEBSPEC_ACCESS_LOG") == "1",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    if inherited is None:
        uvicorn.run(app, host=resolve_bind_host(), port=port, **options)
        return

    host, inherited_port = inherited.socket.getsockname()[:2]
    if inherited_port != port:
        logger.warning("The %s socket listens on port %d, but WEBSPEC_INTERNAL_PORT is %d",
                       inherited.source, inherited_port, port)
    # Nothing is bound here: the server only accepts on the socket it is handed.
    server = graceful_server(app, host=host, port=inherited_port, **options)
    # uvicorn logs no address for sockets it is handed. A service manager holds the port while
    # it restarts the gateway; any other holder, such as the Docker image's init, holds it while
    # the gateway shuts down, and then exits with it.
    kept = "restarts" if inherited.source in activation.SERVICE_MANAGERS else "shuts down"
    logger.info("Listening on http://%s (socket held by %s: it stays bound while the gateway %s)",
                inherited.address, inherited.source, kept)
    server.run(sockets=[inherited.socket])
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
