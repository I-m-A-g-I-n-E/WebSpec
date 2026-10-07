"""Safe read-modify-write for the gateway's config (mcpServers) and its secrets env file.

On a production host (gateway/deploy/), these are /etc/webspec/config.json and
/etc/webspec/gateway.env; elsewhere ~/.claude.json and ~/.env. Every write keeps the file's
mode, owner and group, so an edit can never widen who may read it.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
from pathlib import Path

# Where gateway/deploy/ installs the config and secrets. sudo drops WEBSPEC_CONFIG and resets
# HOME, so on a production host `sudo webspec-ctl …` must find these, not root's dotfiles.
PRODUCTION_DIR = Path("/etc/webspec")
PRODUCTION_CONFIG = PRODUCTION_DIR / "config.json"
PRODUCTION_ENV = PRODUCTION_DIR / "gateway.env"

# A name the env file may hold: a shell identifier, which systemd's EnvironmentFile= and the
# macOS daemon's `set -a; . gateway.env` both accept. Any other name breaks the macOS daemon's
# start (sh aborts under set -e) and is ignored by systemd.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# WEBSPEC_* variables configure the gateway itself (WEBSPEC_HOST, WEBSPEC_AUDIT_LOG,
# WEBSPEC_GUARD_KEY, ...): they are not MCP secrets. A placeholder `WEBSPEC_AUDIT_LOG=` would turn
# the audit log off, and `WEBSPEC_GUARD_KEY=` after the key would blank it (GD-5). An empty
# WEBSPEC_HOST means loopback (127.0.0.1), as unset does, but the variable decides which
# interfaces the gateway listens on (DP-8): its value is the operator's, never a placeholder's.
_GATEWAY_PREFIX = "WEBSPEC_"


def is_production_host() -> bool:
    """True exactly when /etc/webspec/config.json is a regular file: the production install.

    A host that only has /etc/webspec (for allowed_signers, say) is a development host, whose
    gateway reads WEBSPEC_CONFIG or ~/.claude.json. A config.json that cannot even be looked
    at counts as present: install.sh closes /etc/webspec (0750 root:webspec), and a user who
    cannot see into it must be told to use sudo rather than silently get ~/.claude.json.
    """
    try:
        return stat.S_ISREG(os.stat(PRODUCTION_CONFIG).st_mode)
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return False
        raise


def default_config_path() -> Path:
    """WEBSPEC_CONFIG; else /etc/webspec/config.json on a production host; else ~/.claude.json."""
    if os.environ.get("WEBSPEC_CONFIG"):
        return Path(os.environ["WEBSPEC_CONFIG"])
    if is_production_host():
        return PRODUCTION_CONFIG
    return Path.home() / ".claude.json"


def default_env_path() -> Path:
    """WEBSPEC_ENV_FILE; else /etc/webspec/gateway.env on a production host; else ~/.env."""
    if os.environ.get("WEBSPEC_ENV_FILE"):
        return Path(os.environ["WEBSPEC_ENV_FILE"])
    if is_production_host():
        return PRODUCTION_ENV
    return Path.home() / ".env"


def _access(path: Path, default_mode: int = 0o600) -> tuple[int, tuple[int, int] | None]:
    """The mode and (uid, gid) a rewrite of ``path`` must keep; a new file gets ``default_mode``."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return default_mode, None
    return stat.S_IMODE(st.st_mode), (st.st_uid, st.st_gid)


def _write_atomic(path: Path, text: str, mode: int, owner: tuple[int, int] | None) -> None:
    """Write ``text`` to ``path`` through a temp file that has the final mode and owner from birth.

    The temp file is never readable more widely than the result (O_CREAT's mode is masked by the
    umask, so the mode is set exactly before anything is written), a symlink at the temp path is
    refused, and as root the original owner and group are restored, so rewriting
    /etc/webspec/config.json (root:webspec 0640) or gateway.env (root:root 0600) keeps them.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        if owner is not None and os.geteuid() == 0:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "w") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _read_claude_config(path: Path | None = None) -> dict:
    """Read and parse the config."""
    p = path or default_config_path()
    with open(p) as f:
        return json.load(f)


def _write_claude_config(data: dict, path: Path | None = None) -> None:
    """Atomic write of the config, with a backup; both keep the original's mode and owner."""
    p = path or default_config_path()

    # Validate before writing
    payload = json.dumps(data, indent=2) + "\n"
    json.loads(payload)  # round-trip validation

    mode, owner = _access(p)
    if p.exists():
        _write_atomic(p.with_suffix(".json.bak"), p.read_text(), mode, owner)
    _write_atomic(p, payload, mode, owner)


def add_service(name: str, entry: dict, path: Path | None = None) -> None:
    """Add or update a service in the config's mcpServers.

    Idempotent: adding an existing service updates it.
    """
    data = _read_claude_config(path)
    data.setdefault("mcpServers", {})[name] = entry
    _write_claude_config(data, path)


def remove_service(name: str, path: Path | None = None) -> bool:
    """Remove a service from the config's mcpServers. Returns whether it was there.

    Idempotent: removing a missing service changes nothing and returns False.
    """
    data = _read_claude_config(path)
    servers = data.get("mcpServers", {})
    if name not in servers:
        return False
    del servers[name]
    _write_claude_config(data, path)
    return True


def list_services(path: Path | None = None) -> dict[str, dict]:
    """Return the config's current mcpServers."""
    data = _read_claude_config(path)
    return data.get("mcpServers", {})


def check_env_name(key: str) -> str:
    """Return ``key`` if the env file may hold it as an MCP server secret; raise ValueError if not."""
    if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
        raise ValueError(f"{key!r} is not an environment variable name ([A-Za-z_][A-Za-z0-9_]*)")
    if key.startswith(_GATEWAY_PREFIX):
        raise ValueError(f"{key} is a gateway setting, not an MCP server secret; "
                         f"webspec-ctl does not add or remove {_GATEWAY_PREFIX}* variables")
    return key


def _systemd_env_files() -> list[Path]:
    """The env files that a systemd unit of this repository reads with EnvironmentFile=.

    The production gateway's (webspec-gateway.service, gateway/deploy/linux) and the development
    unit's, ~/.webspec/gateway.env (gateway/systemd).
    """
    files = [PRODUCTION_ENV]
    try:
        files.append(Path.home() / ".webspec" / "gateway.env")
    except (KeyError, RuntimeError):  # no home directory to speak of
        pass
    return files


def env_file_sourced(path: Path | None = None) -> bool:
    """Whether ``export KEY=value`` in the env file at ``path`` sets KEY for what reads the file.

    ``path`` defaults to :func:`default_env_path`. False for a file that systemd reads with
    EnvironmentFile=, on Linux (:func:`_systemd_env_files`): systemd sets KEY from ``KEY=value``
    lines only, ignores an ``export KEY=value`` line, and logs it, value included, to the unit's
    journal at every start. True for every other file, where both forms set KEY: on macOS the
    gateway.env that the LaunchDaemon's /bin/sh sources (``set -ae; . /etc/webspec/gateway.env``,
    gateway/deploy/macos), and a development host's ~/.env, which no unit of this repository
    reads (a shell sources it).
    """
    if sys.platform == "darwin":
        return True
    target = os.path.realpath(path or default_env_path())
    return all(os.path.realpath(f) != target for f in _systemd_env_files())


def env_assignment(key: str, sourced: bool) -> re.Pattern[str]:
    """The start of a line that sets ``key``, to ``match``: ``KEY=…``, and ``export KEY=…`` too if ``sourced``."""
    export = r"(?:export\s+)?" if sourced else ""
    return re.compile(rf"\s*{export}{re.escape(key)}\s*=")


def _env_lines(path: Path) -> list[str]:
    """The lines of an env file; none for a file that cannot be read."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def ignored_exports(key: str, path: Path | None = None, sourced: bool | None = None) -> list[int]:
    """The numbers of the env file's ``export KEY=…`` lines, where what reads it ignores them (systemd).

    [] where the export form sets KEY too (``sourced``, by default :func:`env_file_sourced`), and
    for a file that cannot be read.
    """
    p = path or default_env_path()
    if env_file_sourced(p) if sourced is None else sourced:
        return []
    pattern = re.compile(rf"\s*export\s+{re.escape(key)}\s*=")
    return [number for number, line in enumerate(_env_lines(p), 1) if pattern.match(line)]


def ignored_export_warning(key: str, path: Path | None = None, sourced: bool | None = None) -> str | None:
    """What to tell the operator about the env file's ``export KEY=…`` lines that systemd ignores; None if none.

    It names the lines, never a value (they may hold a secret), and says what to change in
    place: without a ``KEY=value`` line, an export line becomes one; with one, that is the line
    systemd reads, and the export lines go. It never asks for a line after them: systemd keeps
    the last assignment, so a ``KEY=`` there would blank the secret once the export line is
    fixed (add_env_var adds none).
    """
    p = path or default_env_path()
    numbers = ignored_exports(key, p, sourced)
    if not numbers:
        return None
    plain = env_assignment(key, sourced=False)
    read = [number for number, line in enumerate(_env_lines(p), 1) if plain.match(line)]
    many = len(numbers) > 1
    where = f"lines {', '.join(map(str, numbers))}" if many else f"line {numbers[0]}"
    ignored = (f"which systemd ignores: it reads KEY=value lines only, and logs "
               f"{'those lines, values' if many else 'that line, value'} included, to the journal at every start")
    if not read:
        fix = (f"Keep one of {where}, as {key}=value, and delete the others" if many
               else f"Change {where} to {key}=value")
        return (f"{p} sets {key} only with `export {key}=…` ({where}), {ignored}. The gateway does not get {key} "
                f"from this file. {fix}")
    return (f"{p} sets {key} with `export {key}=…` too ({where}), {ignored}. The gateway gets {key} from line "
            f"{read[-1]}. Give line {read[-1]} the value ({key}=value), and delete {where}")


def add_env_var(key: str, value: str = "", path: Path | None = None) -> bool:
    """Add a ``KEY=value`` line to the secrets env file unless a line sets KEY already; return whether it did.

    Never overwrites (idempotent). A line in either form counts, ``export KEY=…`` included,
    whatever reads the file. Where a shell sources it (macOS, a development host's ~/.env), the
    export form sets KEY, and an empty ``KEY=`` after it would blank the secret at the next
    start. Where systemd reads it (:func:`env_file_sourced`), the export form sets nothing, and
    :func:`ignored_export_warning` says to make that line ``KEY=value``: a placeholder after it
    would then be the last assignment, which systemd keeps, and blank the secret all the same.
    Raises ValueError for a name that is not a shell identifier, or that is a WEBSPEC_* setting.
    """
    check_env_name(key)
    p = path or default_env_path()
    lines = p.read_text().splitlines() if p.exists() else []

    pattern = env_assignment(key, sourced=True)  # either form
    if any(pattern.match(line) for line in lines):
        return False  # set already, or set in a form the warning names

    lines.append(f"{key}={value}")
    _write_atomic(p, "\n".join(lines) + "\n", *_access(p))
    return True


def remove_env_var(key: str, path: Path | None = None) -> bool:
    """Remove every line that sets ``key`` (``export`` form included). Returns whether one was there.

    The export form goes on every platform: where systemd ignores it, it still holds the secret.
    Idempotent: removing a missing key changes nothing and returns False. Raises ValueError as
    add_env_var does.
    """
    check_env_name(key)
    p = path or default_env_path()
    if not p.exists():
        return False

    pattern = env_assignment(key, sourced=True)  # every form
    lines = p.read_text().splitlines()
    filtered = [line for line in lines if not pattern.match(line)]
    if len(filtered) == len(lines):
        return False
    _write_atomic(p, "\n".join(filtered) + "\n", *_access(p))
    return True
