from __future__ import annotations

from .records import AccountRecord


def read_accounts(op_client) -> list[AccountRecord]:
    """Inventory 1Password using metadata only.

    Uses list_vaults() + list_items(vault). Deliberately NEVER calls read() or get_item(),
    so no secret field value ever enters this process. Field *values* are ignored even if a
    list_items payload includes them.
    """
    accounts: list[AccountRecord] = []
    for vault in op_client.list_vaults():
        vname = vault.get("name") or vault.get("id") or ""
        for item in op_client.list_items(vname):
            urls = tuple(u.get("href", "") for u in item.get("urls", []) if u.get("href"))
            # field *labels* only — never values
            fields_present = tuple(
                f.get("label") or f.get("id") or "" for f in item.get("fields", [])
            )
            accounts.append(AccountRecord(
                vault=vname,
                item=item.get("id") or item.get("title") or "",
                title=item.get("title", ""),
                category=item.get("category", ""),
                urls=urls,
                fields_present=tuple(fp for fp in fields_present if fp),
                updated_at=item.get("updated_at"),
            ))
    return accounts

# TODO(C): balance/quota/liveness enrichment — per-provider billing calls to answer
# "does this API key still have credit / an active account". One adapter per provider.
# TODO(C): link_service(account, catalog) matching by URL/title to a gateway service.
