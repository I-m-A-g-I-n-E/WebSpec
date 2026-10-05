from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolRecord:
    service: str
    tool: str
    description: str
    verb: str
    noun: str
    tier: str  # "open" | "sensitive" | "dangerous"
    input_schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountRecord:
    vault: str
    item: str
    title: str
    category: str
    urls: tuple[str, ...] = ()
    fields_present: tuple[str, ...] = ()
    updated_at: str | None = None
    linked_service: str | None = None
