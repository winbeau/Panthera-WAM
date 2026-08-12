from __future__ import annotations

import json
import sys
from pathlib import Path

import grpc
import pyarrow.parquet as pq
import pytest
from panthera_arm import camera_pb2_grpc

from armd.backend import SimBackend
from armd.camera.backend import CameraRole, CameraWorker, SimCameraBackend, SimOverheadCameraBackend
from armd.camera.service import CameraService
from armd.collectord.collector import CollectorConfig, collect_episode
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


def test_collector_grpc_limits_cover_full_resolution_depth_frames() -> None:
    config = CollectorConfig(
        arm_endpoint="127.0.0.1:1",
        overhead_endpoint="127.0.0.1:2",
        wrist_endpoint="127.0.0.1:3",
        collection_root=Path("/tmp/unused"),
        episode_id="unused",
        canonical_task="unused",
        operator="pytest",
        panthera_wam_commit="a" * 40,
        calibration={},
        identity={},
    )
    options = dict(config.grpc_options)
    assert options["grpc.max_receive_message_length"] >= 16 * 1024 * 1024
    assert options["grpc.max_send_message_length"] >= 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_collectord_sim_writes_atomic_dual_rgb_depth_episode(tmp_path: Path) -> None:
    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0)
    arm_server = ArmdServer(hardware_loop, bind="127.0.0.1:0")
    hardware_loop.start()
    await arm_server.start()

    wrist_worker = CameraWorker(lambda: SimCameraBackend(width=8, height=6, fps=60))
    wrist_server = grpc.aio.server()
    camera_pb2_grpc.add_CameraServiceServicer_to_server(
        CameraService(wrist_worker),
        wrist_server,
    )
    wrist_port = wrist_server.add_insecure_port("127.0.0.1:0")
    wrist_worker.start()
    await wrist_server.start()

    overhead_worker = CameraWorker(
        lambda: SimOverheadCameraBackend(fps=60),
        role=CameraRole.OVERHEAD,
    )
    overhead_server = grpc.aio.server()
    camera_pb2_grpc.add_CameraServiceServicer_to_server(
        CameraService(overhead_worker),
        overhead_server,
    )
    overhead_port = overhead_server.add_insecure_port("127.0.0.1:0")
    overhead_worker.start()
    await overhead_server.start()

    config = CollectorConfig(
        arm_endpoint=f"127.0.0.1:{arm_server.port}",
        overhead_endpoint=f"127.0.0.1:{overhead_port}",
        wrist_endpoint=f"127.0.0.1:{wrist_port}",
        collection_root=tmp_path,
        episode_id="sim-episode-0001",
        canonical_task="Move the red block from the start area to the target area.",
        operator="pytest",
        panthera_wam_commit="77cc8aad0b2e278bab6202d1fc61c79a1af18424",
        calibration={
            "version": "sim-calibration-v1",
            "workspace_roi": [0, 0, 8, 6],
            "synthetic": True,
        },
        identity={
            "dataset_id": "sim-dataset-v1",
            "task_id": "sim-task-v1",
            "calibration_version": "sim-calibration-v1",
            "camera_mount_version": "sim-mount-v1",
            "roi_version": "sim-roi-v1",
            "action_units_version": "panthera-7axis-rad-nativegripper-v1",
        },
        duration_s=0.4,
        capture_depth=True,
        allow_unapproved_root=True,
        expected_overhead_serial="SIM-C920E-0001",
        expected_wrist_serial="SIM-D405-0001",
    )
    try:
        episode_path = await collect_episode(config)
    finally:
        await arm_server.stop()
        hardware_loop.stop()
        await wrist_server.stop(0)
        wrist_worker.stop()
        await overhead_server.stop(0)
        overhead_worker.stop()

    assert (episode_path / "COMPLETE").is_file()
    assert not (episode_path / "FAILED.json").exists()
    table = pq.read_table(episode_path / "samples.parquet")
    assert table.num_rows >= 2
    assert len(list((episode_path / "overhead").glob("*"))) == table.num_rows
    assert len(list((episode_path / "wrist_rgb").glob("*.png"))) == table.num_rows
    assert len(list((episode_path / "wrist_depth").glob("*.png"))) == table.num_rows
    sync = json.loads((episode_path / "sync_report.json").read_text(encoding="utf-8"))
    assert sync["valid_ticks"] == sync["canonical_ticks"] == table.num_rows
    assert all(value == 0 for value in sync["sequence_gaps"].values())
    assert all(value == 0 for value in sync["ring_overflows"].values())
    episode = json.loads((episode_path / "episode.json").read_text(encoding="utf-8"))
    assert episode["action_semantics"] == "next_absolute_position_waypoint_q_t_plus_1_30hz"
    assert episode["depth"] == {"requested": True, "complete": True}
    assert episode["camera_state_offset_frames"] is None
    assert episode["offset_estimation_method"] == "insufficient_motion"

    tools_src = Path(__file__).resolve().parents[3] / "tools/lerobot-v3/src"
    sys.path.insert(0, str(tools_src))
    try:
        from panthera_lerobot_v3.packager import validate_staging_episode

        validated_episode, validated_samples = validate_staging_episode(episode_path)
    finally:
        sys.path.remove(str(tools_src))
    assert validated_episode["episode_id"] == "sim-episode-0001"
    assert len(validated_samples) == table.num_rows
