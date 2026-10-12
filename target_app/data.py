"""In-memory member data for the mock core banking system.

All people, SSNs and balances are fictional. SSNs use the 900-series, which is
never issued, so they are obviously fake while still looking like real PII to
the redaction layer.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

SEED_DIR = Path(__file__).parent / "seed"

# code -> (description, minimum opening deposit)
SHARE_TYPES: dict[str, tuple[str, Decimal]] = {
    "REG": ("Regular Savings", Decimal("5.00")),
    "XMAS": ("Holiday Club", Decimal("0.00")),
    "MMKT": ("Money Market", Decimal("2500.00")),
}


def money(value: Decimal | str) -> str:
    return f"${Decimal(value):,.2f}"


def parse_amount(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if amount < 0 or amount.as_tuple().exponent < -2:
        return None
    return amount


@dataclass
class BankState:
    members: dict[str, dict] = field(default_factory=dict)
    users: dict[str, dict] = field(default_factory=dict)
    next_confirmation: int = 40217731

    @classmethod
    def load(cls) -> "BankState":
        members = json.loads((SEED_DIR / "members.json").read_text(encoding="utf-8"))
        users = json.loads((SEED_DIR / "users.json").read_text(encoding="utf-8"))
        return cls(
            members={m["member_no"]: copy.deepcopy(m) for m in members},
            users={u["user_id"]: u for u in users},
        )

    def search(self, member_no: str, last_name: str) -> list[dict]:
        results = []
        for m in self.members.values():
            if member_no and m["member_no"] != member_no:
                continue
            if last_name and m["last_name"].lower() != last_name.lower():
                continue
            results.append(m)
        return sorted(results, key=lambda m: m["member_no"])

    def add_share(self, member_no: str, type_code: str, nickname: str, deposit: Decimal) -> tuple[str, str]:
        member = self.members[member_no]
        suffix = f"{max(int(a['suffix']) for a in member['accounts']) + 1:04d}"
        description = nickname or SHARE_TYPES[type_code][0]
        member["accounts"].append(
            {
                "suffix": suffix,
                "type": "SHARE",
                "description": description,
                "balance": str(deposit),
                "available": str(deposit),
            }
        )
        confirmation = f"CU-{self.next_confirmation}"
        self.next_confirmation += 1
        return suffix, confirmation
