"""Deployment identity allow-list for hardware policy assets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PolicyAssetError(ValueError):
    pass


def _digest(value: Any, *, field: str) -> str:
    result = str(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise PolicyAssetError(f"{field} must be a lowercase SHA-256 hex digest")
    return result


@dataclass(frozen=True, slots=True)
class PolicyAssetIdentity:
    checkpoint_sha256: str
    stats_sha256: str
    schema_sha256: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PolicyAssetIdentity":
        return cls(
            checkpoint_sha256=_digest(value.get("checkpoint_sha256"), field="checkpoint_sha256"),
            stats_sha256=_digest(value.get("stats_sha256"), field="stats_sha256"),
            schema_sha256=_digest(value.get("schema_sha256"), field="schema_sha256"),
        )


class PolicyAssetAllowList:
    def __init__(self, identities: tuple[PolicyAssetIdentity, ...]) -> None:
        if not identities:
            raise PolicyAssetError("policy asset allow-list must contain at least one identity")
        self._identities = frozenset(identities)

    @classmethod
    def load(cls, path: str | Path) -> "PolicyAssetAllowList":
        manifest_path = Path(path).expanduser().resolve()
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyAssetError(f"cannot load policy asset allow-list: {manifest_path}") from exc
        if isinstance(value, dict) and "allowed_assets" in value:
            records = value["allowed_assets"]
        elif isinstance(value, list):
            records = value
        else:
            records = [value]
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise PolicyAssetError("policy asset allow-list must be an object or list of objects")
        return cls(tuple(PolicyAssetIdentity.from_mapping(record) for record in records))

    def allows(self, *, checkpoint_sha256: str, stats_sha256: str, schema_sha256: str) -> bool:
        try:
            identity = PolicyAssetIdentity.from_mapping(
                {
                    "checkpoint_sha256": checkpoint_sha256,
                    "stats_sha256": stats_sha256,
                    "schema_sha256": schema_sha256,
                }
            )
        except PolicyAssetError:
            return False
        return identity in self._identities
