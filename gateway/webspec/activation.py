"""The listening socket a service manager hands to the gateway (DP-8, DP-9).

In the production deployments the service manager binds the gateway's port, 127.0.0.1:7002,
and keeps it bound while the gateway restarts, crashes or is upgraded: systemd through
gateway/deploy/linux/webspec-gateway.socket, launchd through the Sockets entry of
gateway/deploy/macos/com.webspec.gateway.daemon.plist. If the gateway bound the port itself,
the port would be free between one process and the next, and any local process, the agent's
included, could take it and receive what Caddy forwards there: full URLs with GET arguments,
request bodies, and the guard, nonce, clearance and approval headers.

``inherited_socket()`` returns that socket, or None when the gateway was started without
one; it then binds WEBSPEC_HOST:WEBSPEC_INTERNAL_PORT itself (python -m webspec in a shell,
the development units). When activation was requested but the socket is missing or is not
what the deployment promises (one TCP stream socket, listening on a loopback address or on
exactly the address WEBSPEC_HOST names), it raises ActivationError: the gateway must then
exit, never bind the port itself.

- systemd, as sd_listen_fds(3) reads it: the sockets are this process's when LISTEN_PID names
  it. LISTEN_FDS must then be 1, the socket being descriptor 3. Variables that name another
  process, or come without LISTEN_PID, were left behind by some parent and are ignored. The
  variables are removed once read either way, so no child process sees them. (Under the
  production unit the gateway is the main process, Type=exec, so LISTEN_PID is its own; and a
  gateway that bound 127.0.0.1:7002 itself there would fail, since systemd holds the port.)
- launchd: WEBSPEC_LAUNCHD_SOCKET names the socket in the job's Sockets dictionary (the
  daemon plist sets it to "gateway"); launch_activate_socket(3) hands it over.
- Any other parent can pass a socket the way systemd does, naming itself in LISTEN_FDNAMES,
  and the messages name that holder. The Docker image's init (docker/init.py --listen,
  LISTEN_FDNAMES=webspec-init) holds 0.0.0.0:7002 inside the container's own network
  namespace, which the gateway accepts because docker/docker-compose.yml sets
  WEBSPEC_HOST=0.0.0.0: an address other than loopback is accepted only when WEBSPEC_HOST
  names exactly that address, the same explicit override that lets the gateway bind it
  itself (DP-8). Only an IP address names it: a host name never does, whatever it resolves
  to. A socket on loopback is accepted whatever WEBSPEC_HOST says.

systemd and launchd restart the gateway and hold the port in between (SERVICE_MANAGERS). Any
other holder holds it while the gateway shuts down; the Docker image's init then exits with
the gateway, and the container with it.

The socket is made close-on-exec, so the stdio MCP servers and ssh-keygen that the gateway
starts never inherit it: a process that could accept() on it would receive the gateway's
traffic.
"""

from __future__ import annotations

import ctypes
import errno
import ipaddress
import logging
import os
import socket
import sys
from collections.abc import Callable, MutableMapping
from typing import NamedTuple

logger = logging.getLogger("webspec.activation")

SD_LISTEN_FDS_START = 3  # sd_listen_fds(3): the first descriptor systemd passes
SYSTEMD_VARIABLES = ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "LISTEN_PIDFDID")
LAUNCHD_VARIABLE = "WEBSPEC_LAUNCHD_SOCKET"
# The holders that restart the gateway and keep its port bound in between (Inherited.source).
SERVICE_MANAGERS = ("systemd", "launchd")

# What launch_activate_socket(3)'s errors mean. launch(3) gives ENOENT for a name the job's
# Sockets dictionary lacks and ESRCH for a process launchd does not manage, but launchd
# answers ESRCH to both (macOS 27: a job asking for an unknown name gets ESRCH, and so does a
# child of the job's process, whatever the name). The name comes from WEBSPEC_LAUNCHD_SOCKET,
# which the plist sets and gateway.env can override.
LAUNCHD_ERRORS = {
    errno.ENOENT: "the job has no socket by that name: WEBSPEC_LAUNCHD_SOCKET must name a key of its "
                  "Sockets dictionary",
    errno.ESRCH: "this process is not a launchd job, or its job has no socket by that name: "
                 "WEBSPEC_LAUNCHD_SOCKET must name a key of the job's Sockets dictionary, and the job must "
                 "exec the gateway, not start it as a child",
    errno.EALREADY: "the socket was already handed over",
}

# int launch_activate_socket(const char *name, int **fds, size_t *cnt), from <launch.h>.
LaunchActivateSocket = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_size_t))
# void free(void *): launch_activate_socket allocates the array of descriptors; the caller frees it.
Free = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class ActivationError(RuntimeError):
    """Socket activation was requested, but no usable listening socket was passed."""


class Inherited(NamedTuple):
    socket: socket.socket
    source: str  # who holds it: "systemd", "launchd", or the name a parent gave it (LISTEN_FDNAMES)

    @property
    def address(self) -> str:
        return format_address(self.socket)


def format_address(sock: socket.socket) -> str:
    host, port = sock.getsockname()[:2]
    return f"[{host}]:{port}" if sock.family == socket.AF_INET6 else f"{host}:{port}"


def inherited_socket(environ: MutableMapping[str, str] | None = None, pid: int | None = None) -> Inherited | None:
    """The listening socket that systemd or launchd passed in, or None when there is none.

    Raises ActivationError when activation was requested but the socket is unusable.
    """
    env = os.environ if environ is None else environ
    # DP-8's explicit override: a socket bound to exactly this address is accepted as well.
    override = (env.get("WEBSPEC_HOST") or "").strip()
    if any(name in env for name in SYSTEMD_VARIABLES):
        holder = _holder(env.get("LISTEN_FDNAMES"))
        sock = _from_systemd(env, os.getpid() if pid is None else pid, override, holder)
        if sock is not None:
            return Inherited(sock, holder)
    if LAUNCHD_VARIABLE in env:
        return Inherited(_from_launchd(env[LAUNCHD_VARIABLE], override), "launchd")
    return None


def _holder(fdnames: str | None) -> str:
    """Who passed a LISTEN_* socket: the name in LISTEN_FDNAMES, one per descriptor, colon-separated.

    systemd names each socket after its socket unit unless told otherwise.
    """
    names = {name for name in (fdnames or "").split(":") if name}
    if all(name.endswith(".socket") for name in names):
        return "systemd"
    return names.pop() if len(names) == 1 else fdnames


def _from_systemd(env: MutableMapping[str, str], pid: int, override: str = "",
                  holder: str = "systemd") -> socket.socket | None:
    """The socket ``holder`` passed, or None when the variables are not this process's (sd_listen_fds(3))."""
    listen_pid, listen_fds = env.get("LISTEN_PID"), env.get("LISTEN_FDS")
    for name in SYSTEMD_VARIABLES:  # as sd_listen_fds(1) does: read once, then gone for every child
        env.pop(name, None)
    if listen_pid is None:
        logger.warning("Ignoring the LISTEN_* variables: without LISTEN_PID no socket was passed to this process")
        return None
    if not listen_pid.isdigit() or int(listen_pid) <= 0:
        raise ActivationError(f"LISTEN_PID is not a process ID ({listen_pid!r})")
    if int(listen_pid) != pid:
        # A parent that was socket-activated left them in its environment: they are not ours.
        logger.warning("Ignoring LISTEN_PID=%s: %s passed those sockets to that process, not to this one (%d)",
                       listen_pid, holder, pid)
        return None
    if listen_fds != "1":
        unit = ": webspec-gateway.socket must have a single ListenStream= (127.0.0.1:7002)"
        raise ActivationError(f"{holder} passed {listen_fds or 'no'} sockets (LISTEN_FDS); expected exactly one"
                              + (unit if holder == "systemd" else ""))
    return _checked(SD_LISTEN_FDS_START, f"the socket from {holder} (fd 3)", override)


def _launchd_api() -> tuple[Callable, Callable]:
    """launch_activate_socket and free, from libSystem."""
    if sys.platform != "darwin":
        raise ActivationError(f"{LAUNCHD_VARIABLE} is set, but launchd sockets exist on macOS only")
    try:
        libsystem = ctypes.CDLL(None)  # the process's own namespace, which libSystem is part of
        return LaunchActivateSocket(("launch_activate_socket", libsystem)), Free(("free", libsystem))
    except (OSError, AttributeError) as exc:
        raise ActivationError(f"launch_activate_socket is not available: {exc}") from None


def _from_launchd(name: str, override: str = "") -> socket.socket:
    if not name:
        raise ActivationError(f"{LAUNCHD_VARIABLE} is empty; it must name a socket in the job's Sockets dictionary")
    activate, free = _launchd_api()
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t(0)
    err = activate(name.encode(), ctypes.byref(fds), ctypes.byref(count))
    if err:
        hint = LAUNCHD_ERRORS.get(err)
        raise ActivationError(f"launch_activate_socket({name!r}) failed: {os.strerror(err)}"
                              + (f" ({hint})" if hint else ""))
    try:
        got = [fds[i] for i in range(count.value)]
    finally:
        free(ctypes.cast(fds, ctypes.c_void_p))
    if len(got) != 1:
        for fd in got:
            os.close(fd)
        raise ActivationError(f"launchd passed {len(got)} sockets named {name!r}; expected exactly one "
                              "(SockFamily IPv4, SockNodeName 127.0.0.1)")
    return _checked(got[0], f"the launchd socket {name!r}", override)


def _checked(fd: int, what: str, override: str = "") -> socket.socket:
    """The listening TCP socket at fd, made close-on-exec; ActivationError otherwise.

    It must be bound to a loopback address, or to exactly the address ``override``
    (WEBSPEC_HOST) names.
    """
    try:
        sock = socket.socket(fileno=fd)
    except OSError as exc:
        raise ActivationError(f"{what} is not a socket: {exc.strerror or exc}") from None
    try:
        sock.set_inheritable(False)
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type != socket.SOCK_STREAM:
            raise ActivationError(f"{what} is not a TCP stream socket ({_name(sock.family)}, {_name(sock.type)})")
        if not _listening(sock):
            raise ActivationError(f"{what} is not listening")
        host, port = sock.getsockname()[:2]
        if port == 0 or not (_loopback(sock.family, host) or _named(override, host)):
            # A host name never names the address: say so, or it reads as if it should.
            name = "" if not override or _is_address(override) else (
                f"; WEBSPEC_HOST={override!r} is not an IP address, and only an IP address names the address of "
                "a socket passed in")
            raise ActivationError(f"{what} is bound to {format_address(sock)}, not to a loopback address "
                                  "(127.0.0.0/8 or ::1) or the address WEBSPEC_HOST names; the gateway "
                                  f"listens on loopback only unless WEBSPEC_HOST says otherwise (DP-8){name}")
    except OSError as exc:
        sock.close()
        raise ActivationError(f"{what} cannot be used: {exc.strerror or exc}") from None
    except BaseException:
        sock.close()
        raise
    return sock


def _name(value: object) -> str:
    return getattr(value, "name", str(value))


def _listening(sock: socket.socket) -> bool:
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except (AttributeError, OSError):  # macOS cannot tell (ENOPROTOOPT); accept() will
        return True


def _loopback(family: int, host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if family == socket.AF_INET6:
        return address == ipaddress.IPv6Address("::1")
    return address.is_loopback


def _named(override: str, host: str) -> bool:
    """True when WEBSPEC_HOST names exactly this address: DP-8's explicit override."""
    if not override:
        return False
    try:
        return ipaddress.ip_address(override.strip("[]")) == ipaddress.ip_address(host)
    except ValueError:
        return False


def _is_address(override: str) -> bool:
    """Whether WEBSPEC_HOST is an IP address, as _named reads it. A host name is not one."""
    try:
        ipaddress.ip_address(override.strip("[]"))
    except ValueError:
        return False
    return True
