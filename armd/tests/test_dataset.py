from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest
from panthera_arm import dataset_pb2

from armd.dataset_service import DatasetJobManager
from armd.dataset_worker import estimate_fps, feature_spec, lerobot_frame
from armd.teach import TeachStore


def test_lerobot_mapping_and_fps() -> None:
    frames = [
        {"t": 0.0, "pos": [0.0] * 6, "vel": [0.0] * 6, "gripper_pos": 0.1},
        {"t": 0.01, "pos": [0.1] * 6, "vel": [0.2] * 6, "gripper_pos": 0.2},
    ]
    assert estimate_fps(frames) == 100
    mapped = lerobot_frame(frames[1], "pick")
    assert mapped["observation.state"].dtype == np.float32
    assert mapped["observation.state"].shape == (7,)
    assert mapped["action"].tolist() == mapped["observation.state"].tolist()
    assert mapped["task"] == "pick"
    assert feature_spec()["action.velocity"]["shape"] == (7,)


@pytest.mark.asyncio
async def test_dataset_job_runs_isolated_lerobot_worker(tmp_path, monkeypatch) -> None:
    fake_package = tmp_path / "fake" / "lerobot" / "datasets"
    fake_package.mkdir(parents=True)
    (fake_package.parent / "__init__.py").write_text("", encoding="utf-8")
    (fake_package / "__init__.py").write_text(
        """
import json
from pathlib import Path

class LeRobotDataset:
    @classmethod
    def create(cls, **kwargs):
        obj = cls()
        obj.root = Path(kwargs["root"])
        obj.root.mkdir(parents=True)
        obj.count = 0
        return obj

    def add_frame(self, frame):
        self.count += 1

    def save_episode(self):
        pass

    def finalize(self):
        meta = self.root / "meta"
        meta.mkdir(exist_ok=True)
        (meta / "info.json").write_text(json.dumps({"codebase_version": "v3.0", "frames": self.count}))
""",
        encoding="utf-8",
    )
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, (str(tmp_path / "fake"), existing_pythonpath))),
    )
    monkeypatch.setenv("PANTHERA_LEROBOT_RUNNER", sys.executable)

    teach_store = TeachStore(tmp_path / "teach")
    trajectory = teach_store.recording_path("episode.jsonl")
    trajectory.write_text(
        json.dumps({"t": 0.0, "pos": [0.0] * 6, "vel": [0.0] * 6, "gripper_pos": 0.0})
        + "\n"
        + json.dumps({"t": 0.01, "pos": [0.01] * 6, "vel": [0.1] * 6, "gripper_pos": 0.1})
        + "\n",
        encoding="utf-8",
    )
    manager = DatasetJobManager(teach_store=teach_store, root=tmp_path / "datasets")
    job = manager.start(
        trajectory_path=str(trajectory),
        output_dir="episode-v3",
        repo_id="local/test",
        task_name="pick",
        overwrite=False,
    )
    assert job.task is not None
    await job.task
    assert job.state == dataset_pb2.DATASET_JOB_STATE_DONE
    assert job.progress == 1.0
    assert job.frame_count == 2
    assert (job.output / "meta" / "info.json").is_file()
    assert (job.output / "panthera-source.json").is_file()
