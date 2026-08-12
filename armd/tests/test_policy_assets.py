from __future__ import annotations

import json
from pathlib import Path

import pytest

from armd.policy_assets import PolicyAssetAllowList, PolicyAssetError

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def test_policy_asset_allow_list_requires_exact_triple(tmp_path: Path) -> None:
    path = tmp_path / "policy-assets.json"
    path.write_text(
        json.dumps(
            {
                "allowed_assets": [
                    {
                        "checkpoint_sha256": HASH_A,
                        "stats_sha256": HASH_B,
                        "schema_sha256": HASH_C,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    allow_list = PolicyAssetAllowList.load(path)
    assert allow_list.allows(
        checkpoint_sha256=HASH_A,
        stats_sha256=HASH_B,
        schema_sha256=HASH_C,
    )
    assert not allow_list.allows(
        checkpoint_sha256=HASH_A,
        stats_sha256=HASH_A,
        schema_sha256=HASH_C,
    )
    assert not allow_list.allows(
        checkpoint_sha256="bad",
        stats_sha256=HASH_B,
        schema_sha256=HASH_C,
    )


def test_policy_asset_allow_list_rejects_empty_or_malformed_manifest(tmp_path: Path) -> None:
    path = tmp_path / "policy-assets.json"
    path.write_text(json.dumps({"allowed_assets": []}), encoding="utf-8")
    with pytest.raises(PolicyAssetError, match="at least one"):
        PolicyAssetAllowList.load(path)

    path.write_text(json.dumps({"checkpoint_sha256": "bad"}), encoding="utf-8")
    with pytest.raises(PolicyAssetError, match="checkpoint_sha256"):
        PolicyAssetAllowList.load(path)
