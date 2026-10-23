"""Definer validation (DF-1 to DF-3): the ``WebSpec-Definer`` header.

``POST`` must carry a definer; ``PUT``, ``PATCH`` and ``DELETE`` may. A definer that is sent
is exactly one verb of the request method's family, matched without regard to (ASCII) case.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFINER_HEADER = "WebSpec-Definer"

# Definer verb families — which verbs belong to which HTTP method
DEFINER_FAMILIES: dict[str, set[str]] = {
    "POST": {"CREATE", "SEND", "INVOKE", "TRIGGER", "UPLOAD"},
    "PUT": {"REPLACE", "OVERWRITE", "SET"},
    "PATCH": {"MODIFY", "APPEND", "AMEND", "RENAME"},
    "DELETE": {"REMOVE", "REVOKE", "ARCHIVE", "CANCEL", "PURGE"},
}

# Reverse lookup: verb → method family
VERB_TO_FAMILY: dict[str, str] = {}
for method, verbs in DEFINER_FAMILIES.items():
    for verb in verbs:
        VERB_TO_FAMILY[verb] = method

# DF-1: the method that must carry a definer, and the methods that may.
DEFINER_REQUIRED_METHODS = frozenset({"POST"})
DEFINER_METHODS = frozenset(DEFINER_FAMILIES)


@dataclass
class DefinerResult:
    canonical: str  # the verb in upper case (e.g. "INVOKE"); empty when none was sent


@dataclass
class DefinerError:
    error_type: str  # "missing_definer", "unknown_definer", "definer_family_mismatch"
    detail: str
    status_code: int  # HTTP status code to return


def definer_header(headers) -> str | None:
    """The definer exactly as received, or None when the header is absent.

    Repeated header lines are combined with ", " (RFC 9110 §5.3), so they reach DF-2 as one
    value with a comma and are refused, and the guard (GD-2) signs the same value that is
    validated here.
    """
    values = headers.getlist(DEFINER_HEADER)
    return ", ".join(values) if values else None


def validate_definer(method: str, header_value: str | None) -> DefinerResult | DefinerError:
    """Validate the definer of a request (DF-1, DF-2).

    ``header_value`` is :func:`definer_header`'s result: None when the header is absent.
    """
    method = method.upper()
    if method not in DEFINER_METHODS:
        return DefinerResult(canonical="")

    if header_value is None:
        if method in DEFINER_REQUIRED_METHODS:
            return DefinerError(
                error_type="missing_definer",
                detail=f"{method} requires the {DEFINER_HEADER} header",
                status_code=400,
            )
        return DefinerResult(canonical="")

    # DF-2: exactly one verb token. An empty value, the old "VERB:bookend" form, repeated
    # lines combined with a comma, or a word in no family are all unknown definers. Only the
    # spaces and tabs around a field value (RFC 9110 §5.5) are not part of it: str.strip() would
    # also remove characters that h11 passes in a value (\x1c-\x1f, \x85, \xa0), so a value that
    # an intermediary sees as something else would still select the verb.
    verb = header_value.strip(" \t")
    verb = verb.upper() if verb.isascii() and verb.isalpha() else ""
    if verb not in VERB_TO_FAMILY:
        return DefinerError(
            error_type="unknown_definer",
            detail=f"{DEFINER_HEADER} must be exactly one verb of {method}'s family: "
                   f"{', '.join(sorted(DEFINER_FAMILIES[method]))}",
            status_code=400,
        )

    expected_family = VERB_TO_FAMILY[verb]
    if expected_family != method:
        return DefinerError(
            error_type="definer_family_mismatch",
            detail=f"{verb} belongs to {expected_family} family, not {method}",
            status_code=400,
        )

    return DefinerResult(canonical=verb)
