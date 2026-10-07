"""PID 1 of the gateway container: reap orphans, forward signals, stay non-dumpable.

docker/Dockerfile makes this the image's entrypoint, in front of docker/entrypoint.sh:

    python3 -I init.py (PID 1) -> entrypoint.sh -> exec python -I -m webspec

Orphans are reparented to PID 1. Without an init, the gateway would be PID 1, and a process
that a stdio MCP server leaves behind would stay a zombie of the gateway, which never waits
for it, until the container restarts. This init waits for every child.

It is not Docker's own init (`init: true`), because docker-init runs as the container's user
and stays dumpable: every stdio MCP server, which runs as the same uid 10001, could read the
container's whole environment, guard key and service secrets included, from
/proc/1/environ. This init holds that environment too, so it makes itself non-dumpable
(DP-2, docs/spec/audit-deployment.md) before it starts anything, and refuses to start
anything if it cannot. The gateway does the same for itself at startup. For the same reason it
refuses to start anything under another PID 1 whose environment this user can read, such as
docker-init added by `docker run --init` or a daemon-wide "init": true.

What it does:
- with --listen, binds the gateway's address, WEBSPEC_HOST (empty meaning 127.0.0.1) at
  WEBSPEC_INTERNAL_PORT, before it starts anything, and refuses to start anything if it cannot,
  or if WEBSPEC_HOST is not an IP address. When that address is a wildcard, such as 0.0.0.0, it
  also binds the other address family's wildcard at the port, without listening there;
- starts its arguments as its only child, with every signal at its default action and none
  blocked. With --listen, the child gets the listening socket as descriptor 3, with
  LISTEN_FDS=1 and LISTEN_PID set to its own PID, as systemd passes a socket
  (webspec/activation.py takes it);
- forwards SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGUSR1, SIGUSR2 and SIGWINCH to that child, so
  `docker stop` reaches the gateway;
- continues that child at once when a signal, such as SIGSTOP, stops it. The gateway is not
  PID 1, so the stdio servers, which share its user, can stop it, and nothing else would ever
  continue it: the container would stay up, serving nothing, and never restart;
- waits for every child that exits, its own and orphans alike;
- exits when its child exits, with the child's exit code, or 128 + N if signal N killed it.
  With --listen, as PID 1, it first kills every other process in the container and waits until
  none is left, for END_EVERYONE_ELSE_SECONDS at most: a process of another user, such as a root
  `docker exec`, is out of its reach. The kernel kills whatever is left once the init has exited.

Why --listen: the stdio servers share the gateway's user and network namespace, so they can
end the gateway with SIGTERM. uvicorn then closes its listening socket first and waits for the
requests in progress, up to 35 seconds (a tool call's 30, and 5 more), before it exits. If the gateway owned the
port, it would be free all that time inside a container that is still running, and a stdio
server could listen on it and receive what Caddy forwards there for every destination: GET
arguments, request bodies, and the guard, nonce, clearance and approval headers. This init's
copy of the socket keeps the port bound for the container's life: connections that arrive while
the gateway shuts down wait in the backlog, and are reset when the container exits. The kernel
kills the container's other processes only once PID 1 has exited, after it has closed the
socket, and a process a stdio server left behind could take the port in between. So once the
gateway has exited, the init kills every other process itself, and lets the port go only when
none is left, or after END_EVERYONE_ELSE_SECONDS if a process of another user is left, which no
stdio server can start.

The image passes --listen: the gateway takes a socket bound to the address WEBSPEC_HOST names
(0.0.0.0 in compose), DP-8's explicit override (webspec/activation.py). Apart from one bound to
a loopback address, it takes no other, so WEBSPEC_HOST must be an IP address: a host name never
names the bound address exactly.

On a wildcard address the port is held in both address families. With the gateway on 0.0.0.0
alone, nothing would hold [::]:7002, and on a network with a global IPv6 prefix Caddy dials the
gateway's IPv6 address first: a stdio server listening there would receive what Caddy sends.
Bound there without listening, the init refuses those connections, and Caddy falls back to
IPv4. docker-compose.yml also keeps the stack's network IPv4-only.

Standard library only; the image runs it as `python3 -I`.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import ipaddress
import os
import signal
import socket
import sys
import time

PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36

# The environment of the container's PID 1 (see pid1_environment_readable).
PID1_ENVIRON = "/proc/1/environ"

# sd_listen_fds(3): the first descriptor a passed socket takes in the new program.
SD_LISTEN_FDS_START = 3

# For a gateway on one family's wildcard address, the other family and its wildcard, where the
# init holds the port too (see holding_socket).
OTHER_WILDCARD = {"0.0.0.0": (socket.AF_INET6, "::"), "::": (socket.AF_INET, "0.0.0.0")}

# How long the init, as PID 1, waits for the container's other processes to be gone once its
# child has exited, before it lets the gateway's port go anyway (see end_everyone_else).
END_EVERYONE_ELSE_SECONDS = 2.0

FORWARDED = (
    signal.SIGHUP,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGUSR1,
    signal.SIGUSR2,
    signal.SIGWINCH,
)
WAITED = (*FORWARDED, signal.SIGCHLD)


def _log(message: str) -> None:
    print(f"webspec-init: {message}", file=sys.stderr, flush=True)


def _prctl(option: int, value: int) -> None:
    # The interpreter's own symbols include libc's prctl; find_library would run ldconfig.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def make_non_dumpable() -> None:
    """DP-2: close /proc/<pid>/environ, /proc/<pid>/mem and ptrace to processes of the same user."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "prctl(PR_SET_DUMPABLE) is Linux-only")
    _prctl(PR_SET_DUMPABLE, 0)


def pid1_environment_readable() -> bool:
    """DP-2: whether PID 1 is another process, whose environment this user can read.

    docker-init, which `docker run --init`, compose's `init: true` and a daemon-wide
    "init": true make PID 1, runs as the container's user and stays dumpable, and it holds the
    container's environment, the same as this process's. Every stdio MCP server, which runs as
    that user too, could read it from /proc/1/environ. Outside a container PID 1 belongs to
    root, and its environment is closed to other users.
    """
    if os.getpid() == 1:
        return False
    try:
        with open(PID1_ENVIRON, "rb"):
            return True
    except OSError:
        return False


def _pid1_command() -> str:
    try:
        with open(os.path.join(os.path.dirname(PID1_ENVIRON), "cmdline"), "rb") as f:
            name = f.read().split(b"\0", 1)[0].decode(errors="replace")
    except OSError:
        name = ""
    return name or "unknown"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def gateway_address(environ) -> tuple[str, int]:
    """Where the gateway listens when it binds the port itself (webspec/__main__.py).

    WEBSPEC_HOST, where an empty value counts as unset and means 127.0.0.1, at
    WEBSPEC_INTERNAL_PORT, falling back to WEBSPEC_PORT, then 7001. ValueError if the port is
    not a number, or if WEBSPEC_HOST is not an IP address: the gateway takes the socket this
    init passes only when it is bound to a loopback address or to exactly the address
    WEBSPEC_HOST names (webspec/activation.py), and a host name names no address exactly.
    """
    host = (environ.get("WEBSPEC_HOST") or "").strip() or "127.0.0.1"
    try:
        address = ipaddress.ip_address(host.strip("[]"))  # as webspec/activation.py reads it
    except ValueError:
        raise ValueError(f"WEBSPEC_HOST {host!r} is not an IP address: the gateway takes the socket this "
                         "init holds only on a loopback address or on exactly the address WEBSPEC_HOST "
                         "names (DP-8). Set it to an IP address, 0.0.0.0 in the compose stack") from None
    port = environ.get("WEBSPEC_INTERNAL_PORT", environ.get("WEBSPEC_PORT", "7001")).strip()
    if not (port.isascii() and port.isdigit() and int(port) <= 65535):
        raise ValueError(f"WEBSPEC_INTERNAL_PORT {port!r} is not a port number")
    return str(address), int(port)


def listening_socket(host: str, port: int) -> socket.socket:
    """A TCP socket listening at host, an IP address, and port, which this process keeps until it exits."""
    family, kind, proto, _, address = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE | socket.AI_NUMERICHOST)[0]
    sock = socket.socket(family, kind, proto)
    try:
        if family == socket.AF_INET6:
            # As uvicorn binds an IPv6 address: that family only.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(address)
        sock.listen(socket.SOMAXCONN)
    except BaseException:
        sock.close()
        raise
    return sock


def holding_socket(family: int, host: str, port: int) -> socket.socket | None:
    """A TCP socket bound to host:port that never listens, or None where there is no such address.

    It keeps the port this process's: no other process can listen there, and the connections
    clients make there are refused. Neither SO_REUSEADDR nor SO_REUSEPORT: either would let
    another socket bind the port too. Where the host has no such address family or address, no
    other process can listen there either.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as e:
        if e.errno == errno.EAFNOSUPPORT:
            return None
        raise
    try:
        if family == socket.AF_INET6:
            # That family only: the gateway's socket holds the port's IPv4 side, and a socket
            # for both could not bind next to it.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        if e.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
            return None
        raise
    except BaseException:
        sock.close()
        raise
    return sock


def _exit_code(status: int) -> int:
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def _pass_socket(sock: socket.socket) -> None:
    """In the forked child: hand sock to the new program as systemd does (sd_listen_fds(3))."""
    if sock.fileno() != SD_LISTEN_FDS_START:
        os.dup2(sock.fileno(), SD_LISTEN_FDS_START)
    os.set_inheritable(SD_LISTEN_FDS_START, True)  # dup2 onto itself would keep close-on-exec
    # LISTEN_FDNAMES names the holder, so the gateway logs who keeps its port bound.
    os.environ.update(LISTEN_FDS="1", LISTEN_PID=str(os.getpid()), LISTEN_FDNAMES="webspec-init")


def _exec_child(argv: list[str], sock: socket.socket | None = None) -> None:
    """In the forked child: give the new program default signal handling, then exec it."""
    try:
        # An ignored signal stays ignored across exec, and Python ignores SIGPIPE and SIGXFSZ.
        # Reset every signal, as runc leaves them for a container's first process.
        for sig in signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        signal.pthread_sigmask(signal.SIG_SETMASK, ())
        if sock is not None:
            _pass_socket(sock)
        os.execvp(argv[0], argv)
    except OSError as e:
        _log(f"cannot start {argv[0]}: {e.strerror}")
        os._exit(127 if e.errno == errno.ENOENT else 126)
    finally:
        os._exit(126)  # never return into the parent's loop


def supervise(argv: list[str], sock: socket.socket | None = None) -> int:
    """Run argv as the only child, forward signals to it, reap every child; return its exit code.

    The child gets sock, if any, as descriptor 3 (see _pass_socket).
    """
    # sigwait() takes a signal only while it is blocked. On macOS, a blocked signal whose
    # default action is to ignore it is discarded, so SIGCHLD and SIGWINCH get a handler. It
    # never runs: the signals stay blocked.
    for sig in (signal.SIGCHLD, signal.SIGWINCH):
        signal.signal(sig, lambda *_: None)
    signal.pthread_sigmask(signal.SIG_BLOCK, WAITED)

    if sys.platform.startswith("linux") and os.getpid() != 1:
        # Not PID 1 (a test, or a container that runs another init): adopt orphans anyway.
        try:
            _prctl(PR_SET_CHILD_SUBREAPER, 1)
        except OSError as e:
            _log(f"cannot become a subreaper ({e.strerror}); orphans go to PID 1")

    # fork and exec rather than posix_spawn: glibc's posix_spawn leaves its internal signals
    # (32 and 33) ignored in the new program, and that would be passed on to the gateway and
    # every stdio server.
    try:
        child = os.fork()
    except OSError as e:
        _log(f"cannot start {argv[0]}: fork failed ({e.strerror})")
        return 126
    if child == 0:
        _exec_child(argv, sock)

    code = None
    while code is None:
        sig = signal.sigwait(WAITED)
        if sig != signal.SIGCHLD:
            try:
                os.kill(child, sig)
            except ProcessLookupError:
                pass
            continue
        # One SIGCHLD can stand for several children: reap until none is left waiting.
        # WUNTRACED also reports a child that a signal stopped, once per stop.
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | os.WUNTRACED)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if pid != child:
                continue  # an orphan: reaped, or left alone if it only stopped
            if os.WIFSTOPPED(status):
                # Nothing else would continue it, and the container would stay up, serving
                # nothing, without a restart.
                _log(f"child {child} stopped by {_signal_name(os.WSTOPSIG(status))}; continuing it")
                try:
                    os.kill(child, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                continue
            code = _exit_code(status)
    return code


def end_everyone_else(command: str, seconds: float = END_EVERYONE_ELSE_SECONDS) -> None:
    """As PID 1: kill every other process in the container, and wait until none is left.

    For after the child, command, has exited, while this process still holds the gateway's port.
    The kernel kills the container's other processes only once PID 1 has exited, after it has
    closed the socket: in between, a process a stdio server left behind could listen on the port
    and receive what Caddy forwards there. Gives up after `seconds`, should a process outlive
    SIGKILL that long, or belong to a user this one cannot signal (a root `docker exec`).

    Does nothing unless this process is PID 1: anywhere else kill(-1) reaches every process of
    its user, inside the container or not.
    """
    if os.getpid() != 1:
        return
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.kill(-1, signal.SIGKILL)  # every process this one may signal, but itself
        except ProcessLookupError:
            return  # none is left, zombies included
        while True:  # reap the processes that were this one's children, or have become them
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if time.monotonic() >= deadline:
            _log(f"other processes are still running {seconds:g} seconds after {command} exited; "
                 "letting its port go anyway")
            return
        time.sleep(0.001)


def start(argv: list[str], listen: bool = False) -> int:
    """With listen, bind the gateway's address first; then supervise argv. All but the DP-2 checks."""
    if not listen:
        return supervise(argv)
    # Never start the gateway without the socket: it would bind the port itself, and the port
    # would be free again while it shuts down.
    try:
        host, port = gateway_address(os.environ)
    except ValueError as e:
        _log(f"refusing to start {argv[0]}: {e}")
        return 1
    try:
        sock = listening_socket(host, port)
    except OSError as e:
        _log(f"refusing to start {argv[0]}: cannot listen on {_join(host, port)} ({e.strerror or e})")
        return 1
    with contextlib.ExitStack() as held:
        held.enter_context(sock)
        host, port = sock.getsockname()[:2]
        holding = ""
        if host in OTHER_WILDCARD:
            # Nothing else may listen on the gateway's port in the other address family either.
            family, wildcard = OTHER_WILDCARD[host]
            try:
                other = holding_socket(family, wildcard, port)
            except OSError as e:
                _log(f"refusing to start {argv[0]}: cannot hold {_join(wildcard, port)} ({e.strerror or e})")
                return 1
            if other is not None:
                held.enter_context(other)
                holding = f", and holding {_join(wildcard, port)}"
        _log(f"listening on {_join(host, port)} for {argv[0]}{holding}; the port stays bound until this "
             "init exits")
        try:
            return supervise(argv, sock)
        finally:
            # While the port is still held: once the sockets close, nothing is left to take it.
            end_everyone_else(argv[0])


def _join(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    listen = args[:1] == ["--listen"]
    if listen:
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        _log("usage: init.py [--listen] COMMAND [ARG...]")
        return 2
    try:
        make_non_dumpable()
    except OSError as e:
        # This process holds the container's environment, guard key included.
        _log(f"refusing to start {args[0]}: cannot make this process non-dumpable ({e.strerror})")
        return 1
    if pid1_environment_readable():
        _log(f"refusing to start {args[0]}: PID 1 ({_pid1_command()}) is another init, and this user "
             "can read its environment, the container's, guard key included, and so can every stdio "
             "MCP server (DP-2). Run this image without one: `init: false` in compose, "
             "`docker run --init=false`")
        return 1
    return start(args, listen)


if __name__ == "__main__":
    sys.exit(main())
