from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from panthera_lerobot_v3.fixture import create_staging_fixture
from panthera_lerobot_v3.packager import pack_staging_episode, validate_staging_episode
from panthera_lerobot_v3.schema import ACTION_SEMANTICS


def test_packager_writes_image_v3_and_q_t_plus_1_actions(tmp_path: Path) -> None:
    staging = create_staging_fixture(tmp_path / "staging")
    output = tmp_path / "dataset"
    manifest = pack_staging_episode(staging, output, repo_id="local/test", vcodec="h264")

    assert (staging / "samples.parquet").is_file()
    assert not (staging / "samples.jsonl").exists()
    assert manifest["frame_count"] == 5
    assert manifest["action_semantics"] == ACTION_SEMANTICS
    dataset = LeRobotDataset("local/test", root=output, video_backend="pyav")
    assert dataset.num_episodes == 1
    assert dataset.num_frames == 5
    rows = [dataset[index] for index in range(dataset.num_frames)]
    assert rows[0]["observation.images.overhead_rgb"].shape == (3, 64, 64)
    assert rows[0]["observation.images.wrist_rgb"].shape == (3, 64, 64)
    for index in range(dataset.num_frames - 1):
        np.testing.assert_allclose(
            rows[index]["action"].numpy(),
            rows[index + 1]["observation.state"].numpy(),
            atol=1e-6,
        )
    assert (output / "aux/depth/000000.png").is_file()
    assert not any("depth" in key for key in dataset.meta.video_keys)


def test_staging_requires_complete_marker(tmp_path: Path) -> None:
    staging = create_staging_fixture(tmp_path / "staging")
    (staging / "COMPLETE").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        validate_staging_episode(staging)


def test_staging_rejects_quality_gate_failure(tmp_path: Path) -> None:
    staging = create_staging_fixture(tmp_path / "staging")
    report_path = staging / "sync_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["sequence_gaps"]["state"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="sequence_gaps"):
        validate_staging_episode(staging)


def test_staging_rejects_wrong_action_semantics(tmp_path: Path) -> None:
    staging = create_staging_fixture(tmp_path / "staging")
    episode_path = staging / "episode.json"
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    episode["action_semantics"] = "same_frame_state"
    episode_path.write_text(json.dumps(episode), encoding="utf-8")
    with pytest.raises(ValueError, match="action_semantics"):
        validate_staging_episode(staging)
