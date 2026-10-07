"""Service registry: parse ~/.claude.json mcpServers and manage session keys."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .hostgrammar import is_label
from .methods import parse_level

logger = logging.getLogger("webspec.config")


@dataclass(frozen=True)
class ServiceEntry:
    name: str  # normalized subdomain label
    original_name: str  # raw key from config
    transport_type: Literal["stdio", "http"]
    # stdio fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http fields
    url: str | None = None
    # http auth headers (env vars resolved at parse time)
    headers: dict[str, str] = field(default_factory=dict)
    # guard: require HMAC + nonce authentication (always true when level >= 1)
    guard: bool = False
    # Phase 2: namespace scheme (e.g. "user", "project")
    namespace: str | None = None
    # Security level 0-4 (docs/spec/levels.md). Raw config value in;
    # normalized to an int by __post_init__ (absent → 1 if guard else 0; invalid → 4).
    level: int | None = None
    # Operator per-tool contract overrides: {tool_name: {read_only, destructive, idempotent,
    # open_world, tier}}. Authoritative over the server's annotations (may loosen).
    tools: dict[str, dict] = field(default_factory=dict)
    # Allowed qualifier labels left of this destination (hostgrammar.py), in canonical order.
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # One source of truth: guard <=> level >= 1. Take the stricter of the two.
        level = parse_level(self.level, guard=self.guard, service=self.name)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "guard", level >= 1)


def normalize_name(raw: str) -> str:
    """Normalize an MCP server name to a valid subdomain label.

    NanoBanana[id=4SNMTV] → nanobanana
    MCP_DOCKER → mcp-docker
    iTerm → iterm
    Notion[id=VABYJJ] → notion
    """
    # Strip bracketed suffixes like [id=4SNMTV]
    name = re.sub(r"\[.*?\]", "", raw).strip()
    # Lowercase
    name = name.lower()
    # Replace underscores and spaces with hyphens
    name = re.sub(r"[_\s]+", "-", name)
    # Remove anything that isn't alphanumeric or hyphen
    name = re.sub(r"[^a-z0-9-]", "", name)
    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)
    # Strip leading/trailing hyphens
    name = name.strip("-")
    return name


def is_public_host(host: str) -> bool:
    """True if a Host header names the public domain (WEBSPEC_DOMAIN) or a host under it.

    Loopback names (``*.localhost``) are local (DP-5); without WEBSPEC_DOMAIN nothing is public.
    """
    public_domain = os.environ.get("WEBSPEC_DOMAIN")
    if not public_domain:
        return False
    hostname = host.split(":")[0]
    return hostname == public_domain or hostname.endswith("." + public_domain)


# Variables never expanded into an MCP server's env or headers: the guard key signs every
# guard tag, bookend and clearance, and no backend may hold it (GD-5).
NEVER_EXPANDED_PREFIX = "WEBSPEC_GUARD_KEY"  # WEBSPEC_GUARD_KEY, WEBSPEC_GUARD_KEY_FILE, ...


def _resolve_env(value: str, service: str = "") -> str:
    """Resolve ${VAR} patterns in a string from environment variables.

    Used for http ``headers`` and stdio ``env`` values, so the config file names a secret
    and the gateway's environment supplies it. Unresolvable variables are left as-is
    (no crash), and so are the guard-key variables, with a warning.
    """
    def expand(m: re.Match) -> str:
        name = m.group(1)
        if name.upper().startswith(NEVER_EXPANDED_PREFIX):
            logger.warning("Service %s: ${%s} is left unexpanded: the guard key is never passed to "
                           "an MCP server", service, name)
            return m.group(0)
        return os.environ.get(name, m.group(0))

    return re.sub(r'\$\{([^}]+)\}', expand, value)


def _resolve_stdio_env(raw: object, service: str = "") -> object:
    """``${VAR}`` expansion for a stdio server's ``env`` (the same as for http headers).

    Only string values are expanded. A malformed ``env`` is passed through unchanged, as
    before, so a bad entry fails when its server is spawned rather than at config load.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        return raw
    return {k: _resolve_env(v, service) if isinstance(v, str) else v for k, v in raw.items()}


def _kind(value: object) -> str:
    """What JSON calls the type of a parsed value, for an error message that never shows the value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _object(value: object, what: str) -> dict:
    """``value`` if it is a JSON object; ValueError naming ``what`` if it is not."""
    if not isinstance(value, dict):
        raise ValueError(f"{what} is {_kind(value)}, not an object")
    return value


def parse_claude_config(path: Path | None = None) -> dict[str, ServiceEntry]:
    """Parse mcpServers from ~/.claude.json, return {normalized_name: ServiceEntry}.

    Raises ValueError, naming the entry, for a file that is valid JSON but not a registry: a
    top level or mcpServers that is not an object, an entry that is not one, an http entry
    without url, headers that are not an object of strings. ServiceRegistry.reload rejects
    such a file as a whole and keeps the last good registry. Within an entry, validation is
    shallow: a bad level fails closed, bad labels allow no qualifiers, and a malformed env or
    tools fails when it is used.
    """
    if path is None:
        path = Path.home() / ".claude.json"

    with open(path) as f:
        data = json.load(f)

    servers = _object(_object(data, "the top level").get("mcpServers", {}), "mcpServers")
    registry: dict[str, ServiceEntry] = {}

    for raw_name, cfg in servers.items():
        name = normalize_name(raw_name)
        if not name:
            continue
        cfg = _object(cfg, f"the entry for {raw_name!r}")

        transport_type = cfg.get("type", "stdio")

        guard = bool(cfg.get("guard", False))
        level = cfg.get("level")  # normalized once, in ServiceEntry.__post_init__
        # Kept verbatim: a malformed value fails closed in methods.effective_contract().
        tools = cfg.get("tools") or {}
        raw_labels = cfg.get("labels") or []
        if isinstance(raw_labels, list) and all(isinstance(x, str) and is_label(x) for x in raw_labels):
            labels = tuple(raw_labels)
        else:
            logger.error("Service %s: 'labels' must be a list of lowercase DNS labels — allowing no qualifiers", name)
            labels = ()

        if transport_type == "http":
            if "url" not in cfg:
                raise ValueError(f"the http entry for {raw_name!r} has no 'url'")
            raw_headers = _object(cfg.get("headers", {}), f"'headers' in the entry for {raw_name!r}")
            for header, value in raw_headers.items():
                if not isinstance(value, str):
                    raise ValueError(f"header {header!r} in the entry for {raw_name!r} is {_kind(value)}, "
                                     "not a string")
            resolved_headers = {k: _resolve_env(v, name) for k, v in raw_headers.items()}
            entry = ServiceEntry(
                name=name,
                original_name=raw_name,
                transport_type="http",
                url=cfg["url"],
                headers=resolved_headers,
                guard=guard,
                namespace=cfg.get("namespace"),
                level=level,
                tools=tools,
                labels=labels,
            )
        else:
            entry = ServiceEntry(
                name=name,
                original_name=raw_name,
                transport_type="stdio",
                command=cfg.get("command"),
                args=cfg.get("args", []),
                env=_resolve_stdio_env(cfg.get("env"), name),
                guard=guard,
                level=level,
                tools=tools,
                labels=labels,
            )

        registry[name] = entry

    return registry


_dev_ephemeral_key: bytes | None = None


class GuardKeyError(RuntimeError):
    """Raised when no guard key is available and none may be safely generated."""


def _derive_guard_key(raw: str) -> bytes:
    """64-hex → raw 32 bytes; anything else → sha256(utf8) so any passphrase works."""
    raw = raw.strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return hashlib.sha256(raw.encode()).digest()


GUARD_KEY_FILE_MAX_BYTES = 4096
GUARD_KEY_FILE_MIN_CHARS = 16


def _invisible(ch: str) -> bool:
    """Whitespace (Unicode's included) or an invisible format character (category Cf)."""
    return ch.isspace() or unicodedata.category(ch) == "Cf"


def _blank(value: str) -> bool:
    """Nothing but whitespace or format characters (see _invisible), or nothing at all."""
    return all(_invisible(ch) for ch in value)


def _key_file_text(data: bytes, path: str) -> str:
    """The key in a key file's bytes (GD-5). Every error names the file, never its content.

    A byte order mark, and whitespace or format characters (Unicode category Cf, such as
    U+200B) around the key are removed: editors and copies from web pages add them. Left in,
    they would silently derive another key, and a file holding nothing else would derive a
    publicly computable one, the SHA-256 of that character. What remains must be one line of
    at least 16 characters with no control or format character in it; 64 hex digits is the
    recommended form. WEBSPEC_GUARD_KEY keeps its own, older rules (see get_session_key).
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} is not UTF-8 text") from None
    start, end = 0, len(text)
    while start < end and _invisible(text[start]):
        start += 1
    while end > start and _invisible(text[end - 1]):
        end -= 1
    text = text[start:end]
    if not text:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} is empty")
    if len(text.splitlines()) > 1:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} holds more than one line")
    bad = next((ch for ch in text if unicodedata.category(ch) in ("Cc", "Cf")), None)
    if bad is not None:
        # The code point of a character the key may not contain, never the key itself.
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} holds a control or format character "
                            f"(U+{ord(bad):04X}) inside the key")
    if len(text) < GUARD_KEY_FILE_MIN_CHARS:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} holds fewer than {GUARD_KEY_FILE_MIN_CHARS} "
                            "characters; use 64 hex digits")
    return text


def _read_guard_key_file(path: str) -> str:
    """The key in ``path`` (see :func:`_key_file_text`). Any problem fails closed with GuardKeyError.

    The file must be a regular file of UTF-8 text: a FIFO, a device or a directory is
    refused without blocking or reading it.
    """
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} is not a regular file")
            data = f.read(GUARD_KEY_FILE_MAX_BYTES + 1)
    except OSError as exc:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} cannot be read: {exc.strerror or exc}") from None
    if len(data) > GUARD_KEY_FILE_MAX_BYTES:
        raise GuardKeyError(f"WEBSPEC_GUARD_KEY_FILE {path!r} is larger than {GUARD_KEY_FILE_MAX_BYTES} bytes")
    return _key_file_text(data, path)


def _env_var_key(raw: str) -> bytes | None:
    """The key a WEBSPEC_GUARD_KEY value derives, or None when the value counts as unset.

    Unset means blank: nothing but whitespace or format characters (_blank). GuardKeyError
    if the value is set but not UTF-8. The one rule for the variable, which get_session_key
    and env_guard_key both apply.
    """
    if _blank(raw):
        return None
    try:
        return _derive_guard_key(raw)
    except UnicodeEncodeError:
        # Bytes that are not UTF-8 reach os.environ as lone surrogates, which cannot be
        # encoded to derive the key. Fail closed like any other unusable key (GD-5),
        # outside the except clause so that no chained exception holds the value.
        pass
    raise GuardKeyError("WEBSPEC_GUARD_KEY is not valid UTF-8")


def _env_var_unset(raw: str) -> str:
    """Why WEBSPEC_GUARD_KEY counts as unset. A variable that is there is not shown, only described."""
    return "WEBSPEC_GUARD_KEY is not set" + (" (it holds only whitespace or invisible characters)" if raw else "")


def env_guard_key() -> bytes:
    """The key in WEBSPEC_GUARD_KEY itself, by get_session_key's rules, and nowhere else.

    For a deployment that passes the key in that variable only: with
    WEBSPEC_REQUIRE_GUARD_KEY=1, which the Linux unit sets, the gateway does not start
    without a usable key there (webspec/__main__.py, GD-5), and never falls back to
    WEBSPEC_GUARD_KEY_FILE or WEBSPEC_GUARD_KEY_DEV_EPHEMERAL. GuardKeyError when the
    variable is unset, blank (_blank) or not UTF-8; the message never holds the value.
    """
    raw = os.environ.get("WEBSPEC_GUARD_KEY", "")
    key = _env_var_key(raw)
    if key is None:
        raise GuardKeyError(_env_var_unset(raw))
    return key


def get_session_key() -> bytes:
    """Return the 32-byte guard key, sourced from the password manager via env.

    Populate WEBSPEC_GUARD_KEY from your vault at launch, e.g.:
        export WEBSPEC_GUARD_KEY=$(op read "op://WebSpec/gateway-guard/key")
    When WEBSPEC_GUARD_KEY is unset or blank, WEBSPEC_GUARD_KEY_FILE names a file holding
    the key instead (read on every call, checked by _key_file_text, same derivation); an
    unreadable, empty or malformed file fails closed, whatever else is set. Fails closed if
    absent (no silent random key), and if WEBSPEC_GUARD_KEY is set but not UTF-8.
    Dev-only escape hatch: WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 mints an insecure in-memory key.

    Blank means nothing but whitespace or format characters (_blank). str.strip() keeps
    U+200B, U+FEFF or U+2060, which a copy from a web page can leave as the whole value, and
    such a value would derive a key anyone can compute, the SHA-256 of those characters
    (GD-5). Any value with a visible character derives as it always has.
    """
    raw = os.environ.get("WEBSPEC_GUARD_KEY", "")
    key = _env_var_key(raw)
    if key is not None:
        return key

    key_file = os.environ.get("WEBSPEC_GUARD_KEY_FILE", "")
    if key_file.strip():
        return _derive_guard_key(_read_guard_key_file(key_file))

    if os.environ.get("WEBSPEC_GUARD_KEY_DEV_EPHEMERAL") == "1":
        global _dev_ephemeral_key
        if _dev_ephemeral_key is None:
            _dev_ephemeral_key = secrets.token_bytes(32)
            print(
                "WARNING: WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1 — using an insecure "
                "in-memory guard key (dev only).",
                file=sys.stderr,
            )
        return _dev_ephemeral_key

    raise GuardKeyError(
        f"{_env_var_unset(raw)}. Source it from your password manager, e.g. "
        "`export WEBSPEC_GUARD_KEY=$(op read 'op://WebSpec/gateway-guard/key')`, "
        "or point WEBSPEC_GUARD_KEY_FILE at a file that holds it. "
        "For local dev only, set WEBSPEC_GUARD_KEY_DEV_EPHEMERAL=1."
    )


class ServiceRegistry:
    """Live registry with config reload support."""

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path(os.environ.get("WEBSPEC_CONFIG", str(Path.home() / ".claude.json")))
        self._config_path = config_path
        self._services: dict[str, ServiceEntry] = {}
        self._signature: tuple[int, ...] | None = None
        self._unreadable: str | None = None  # why the file could not be stat'ed, logged once
        self.reload()

    @staticmethod
    def _signature_of(stat: os.stat_result) -> tuple[int, ...]:
        """What tells one version of the file from another.

        The modification time alone misses a replacement that keeps it (cp -p, install -p,
        rsync -a, touch -r, or two writes within the clock's resolution); the inode and size
        catch those. The change time also moves on chmod and chown, so a file that becomes
        readable is picked up without a restart.
        """
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _stat(self) -> os.stat_result | None:
        try:
            stat = self._config_path.stat()
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc.strerror or exc}"
            if reason != self._unreadable:
                logger.warning("Cannot read the config at %s (%s); %s.", self._config_path, reason,
                               "keeping the last good service registry" if self._services
                               else "serving no services until it can be read")
                self._unreadable = reason
            return None
        if self._unreadable is not None:
            logger.info("The config at %s can be read again.", self._config_path)
            self._unreadable = None
        return stat

    def reload(self) -> bool:
        """Read the config again. True if its services now apply; False if it was rejected.

        Whatever stops a file from loading rejects it as a whole: OSError (an empty Docker
        bind-mount directory where a file was expected, a file that cannot be read), a
        ValueError (malformed JSON, or valid JSON that is not a registry, see
        parse_claude_config), or anything else parse_claude_config raises. One WARNING names
        the file and the error. At startup that leaves an empty registry, so create_app()
        never fails on it. On a later reload the last good registry stays: ~/.claude.json is
        rewritten often, and a half-written file must not tear every service down. The
        file's signature is recorded either way, so a bad file is reported once, not at
        every poll, and its next change is read again.
        """
        stat = self._stat()
        if stat is None:
            return False
        self._signature = self._signature_of(stat)
        try:
            services = parse_claude_config(self._config_path)
        except Exception as exc:  # noqa: BLE001 - whatever the failure, keep the last good registry
            logger.warning(
                "Failed to parse config at %s (%s: %s) — %s.",
                self._config_path, type(exc).__name__, exc,
                "keeping the last good service registry" if self._services else "using empty service registry",
            )
            return False
        self._services = services
        return True

    def check_reload(self) -> bool:
        """Check if config file changed, reload if so. Returns True if reloaded.

        False for a changed file that was rejected (see reload): nothing changed.
        """
        stat = self._stat()
        if stat is None or self._signature_of(stat) == self._signature:
            return False
        return self.reload()

    @property
    def services(self) -> dict[str, ServiceEntry]:
        return self._services

    def get(self, name: str) -> ServiceEntry | None:
        return self._services.get(name)

    def names(self) -> list[str]:
        return list(self._services.keys())
