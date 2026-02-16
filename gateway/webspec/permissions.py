"""Scope enforcement: METHOD:host/path pattern matching.

In-scope: fnmatch-based authorize(), LOCAL_ALLOW_ALL, header generation.
Stubbed: JWT validation, scope negotiation, token revocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch


@dataclass
class ScopeSet:
    """A set of METHOD:host/path permission patterns."""
    patterns: list[str] = field(default_factory=list)


# Default scope set for local development — permits everything
LOCAL_ALLOW_ALL = ScopeSet(patterns=["*:*/*", "*:*"])


def authorize(scopes: ScopeSet, method: str, host: str, path: str) -> bool:
    """Check if the given method + host + path is authorized by any scope pattern.

    Patterns follow METHOD:host/path format with fnmatch globbing.
    """
    target = f"{method.upper()}:{host}/{path.lstrip('/')}"
    for pattern in scopes.patterns:
        if fnmatch(target, pattern):
            return True
    return False


def allowed_methods(scopes: ScopeSet, host: str, path: str) -> list[str]:
    """Return which HTTP methods are allowed for this host/path under the given scopes."""
    methods = []
    for m in ("HEAD", "OPTIONS", "GET", "POST", "PUT", "PATCH", "DELETE"):
        if authorize(scopes, m, host, path):
            methods.append(m)
    return methods


def scope_required(service: str, method: str, tool: str) -> str:
    """Return the scope string required for a given method on a service tool."""
    return f"{method}:{service}.localhost/{tool}"


# ── Stubs for future JWT-based auth ──


def validate_token(bearer_token: str) -> ScopeSet:
    """Parse a JWT bearer token and extract its scope set from the aud claim."""
    raise NotImplementedError("JWT token validation not yet implemented")


def request_scopes(service: str, methods: list[str], paths: list[str]) -> ScopeSet:
    """Negotiate scopes with a service for the given methods and paths."""
    raise NotImplementedError("Scope negotiation not yet implemented")


def revoke_token(jti: str) -> None:
    """Revoke a token by its JTI claim."""
    raise NotImplementedError("Token revocation not yet implemented")
