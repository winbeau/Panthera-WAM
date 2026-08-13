from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import grpc
import pyarrow.parquet as pq
import pytest
from panthera_arm import camera_pb2, camera_pb2_grpc

from armd.backend import SimBackend
from armd.camera.backend import CameraRole, CameraWorker, SimCameraBackend, SimOverheadCameraBackend
from armd.camera.service import CameraService
from armd.collectord.collector import (
    CaptureResult,
    CollectorAborted,
    CollectorConfig,
    _camera_worker,
    _CaptureWorker,
    _materialize_camera_sample,
    _run_capture_workers,
    _state_worker,
    collect_episode,
)
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


@pytest.mark.asyncio
async def test_capture_workers_keep_state_reader_progress_during_blocking_camera_io() -> None:
    state_sequences: list[int] = []
    camera_started = threading.Event()

    def read_state(stop_requested: threading.Event) -> None:
        sequence = 0
        while not stop_requested.wait(0.001):
            sequence += 1
            state_sequences.append(sequence)

    def block_camera(stop_requested: threading.Event) -> None:
        camera_started.set()
        stop_requested.wait()

    workers = [
        _CaptureWorker("state", read_state),
        _CaptureWorker("camera", block_camera),
    ]
    await _run_capture_workers(workers, 0.08)

    assert camera_started.is_set()
    assert len(state_sequences) >= 20
    assert state_sequences == list(range(1, len(state_sequences) + 1))
    assert not any(worker._thread.is_alive() for worker in workers)


def test_capture_worker_cancels_resources_bound_after_stop() -> None:
    class Future:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class Call:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class Channel:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    worker = _CaptureWorker("late-bind", lambda _stop: None)
    worker.request_stop()
    channel = Channel()
    ready_future = Future()
    call = Call()

    assert worker.bind_channel(channel) is False
    assert worker.bind_ready_future(ready_future) is False
    assert worker.bind_call(call) is False
    assert channel.closed is True
    assert ready_future.cancelled is True
    assert call.cancelled is True


@pytest.mark.asyncio
async def test_capture_workers_finish_waits_until_fixed_window_is_ready() -> None:
    stopped = threading.Event()
    finish = asyncio.Event()
    ready = False

    def wait_for_stop(stop_requested: threading.Event) -> None:
        stop_requested.wait()
        stopped.set()

    worker = _CaptureWorker("fixed", wait_for_stop)
    task = asyncio.create_task(
        _run_capture_workers(
            [worker],
            1.0,
            finish_event=finish,
            finish_ready=lambda: ready,
        )
    )
    await asyncio.sleep(0.03)
    finish.set()
    await asyncio.sleep(0.03)
    assert not task.done()
    ready = True
    await asyncio.wait_for(task, timeout=0.5)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_capture_workers_abort_does_not_publish_episode() -> None:
    stopped = threading.Event()
    abort = asyncio.Event()

    def wait_for_stop(stop_requested: threading.Event) -> None:
        stop_requested.wait()
        stopped.set()

    worker = _CaptureWorker("abort", wait_for_stop)
    task = asyncio.create_task(_run_capture_workers([worker], 1.0, abort_event=abort))
    await asyncio.sleep(0.03)
    abort.set()
    with pytest.raises(CollectorAborted, match="aborted by operator"):
        await asyncio.wait_for(task, timeout=0.5)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_capture_workers_fail_fast_and_join_peers() -> None:
    peer_stopped = threading.Event()

    def fail(_stop_requested: threading.Event) -> None:
        raise RuntimeError("boom")

    def wait_for_stop(stop_requested: threading.Event) -> None:
        stop_requested.wait()
        peer_stopped.set()

    workers = [
        _CaptureWorker("failed", fail),
        _CaptureWorker("peer", wait_for_stop),
    ]
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"collector stream failed \(failed\): boom"):
        await _run_capture_workers(workers, 5.0)

    assert time.monotonic() - started < 1.0
    assert peer_stopped.is_set()
    assert not any(worker._thread.is_alive() for worker in workers)


@pytest.mark.asyncio
async def test_capture_workers_propagate_failure_at_duration_boundary() -> None:
    def fail_at_boundary(stop_requested: threading.Event) -> None:
        stop_requested.wait()
        raise RuntimeError("tail failure")

    worker = _CaptureWorker("tail", fail_at_boundary)
    with pytest.raises(RuntimeError, match=r"collector stream failed \(tail\): tail failure"):
        await _run_capture_workers([worker], 0.02)

    assert not worker._thread.is_alive()


@pytest.mark.asyncio
async def test_capture_workers_drain_real_200hz_state_during_large_camera_io(
    tmp_path: Path,
) -> None:
    hardware_loop = HardwareLoop(SimBackend, control_hz=200.0, state_tap_capacity=64)
    arm_server = ArmdServer(hardware_loop, bind="127.0.0.1:0")
    hardware_loop.start()
    await arm_server.start()

    class SlowLargeOverheadBackend(SimOverheadCameraBackend):
        def __init__(self) -> None:
            super().__init__(fps=60)
            self._jpeg = b"\xff\xd8" + b"x" * (512 * 1024) + b"\xff\xd9"

    overhead_worker = CameraWorker(
        SlowLargeOverheadBackend,
        role=CameraRole.OVERHEAD,
        collection_capacity=8,
    )
    overhead_server = grpc.aio.server(
        options=(
            ("grpc.max_receive_message_length", 16 * 1024 * 1024),
            ("grpc.max_send_message_length", 16 * 1024 * 1024),
        )
    )
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
        wrist_endpoint="127.0.0.1:1",
        collection_root=tmp_path,
        episode_id="unused",
        canonical_task="unused",
        operator="pytest",
        panthera_wam_commit="a" * 40,
        calibration={},
        identity={},
    )
    result = CaptureResult()
    raw_dir = tmp_path / "raw"
    workers: list[_CaptureWorker] = []
    state_worker: _CaptureWorker
    state_worker = _CaptureWorker(
        "state",
        lambda _stop: _state_worker(config, result.states, state_worker),
    )
    camera_worker: _CaptureWorker
    camera_worker = _CaptureWorker(
        "overhead",
        lambda _stop: _camera_worker(
            config,
            endpoint=config.overhead_endpoint,
            stream=camera_pb2.CAMERA_STREAM_TYPE_COLOR,
            stream_name="overhead_rgb",
            raw_dir=raw_dir,
            output=result.overhead_rgb,
            worker=camera_worker,
        ),
    )
    workers.extend((state_worker, camera_worker))
    try:
        await _run_capture_workers(workers, 0.6)
    finally:
        await arm_server.stop()
        hardware_loop.stop()
        await overhead_server.stop(0)
        overhead_worker.stop()

    sequences = [sample.sequence for sample in result.states]
    assert len(sequences) >= 50
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
    assert all(sample.tap_oldest_available_sequence <= sample.sequence for sample in result.states)
    assert result.overhead_rgb
    assert all(sample.path.suffix == ".jpg" for sample in result.overhead_rgb)
    assert not any(worker._thread.is_alive() for worker in workers)


def test_materialize_camera_sample_converts_raw_rgb_and_depth_after_capture(tmp_path: Path) -> None:
    from armd.collectord.schema import CameraSample

    common = {
        "sequence": 1,
        "stream_instance_id": "camera",
        "width": 2,
        "height": 1,
        "device_timestamp_raw": 1.0,
        "device_timestamp_unit": "milliseconds",
        "device_clock_domain": "test",
        "host_receive_monotonic_ns": 1,
        "host_publish_monotonic_ns": 2,
        "estimated_capture_monotonic_ns": 1,
        "timestamp_source": "test",
        "timestamp_quality": "test",
    }
    rgb_raw = tmp_path / "rgb.rgb8"
    rgb_raw.write_bytes(bytes((255, 0, 0, 0, 255, 0)))
    rgb = CameraSample(
        stream_name="wrist_rgb",
        path=rgb_raw,
        pixel_format="rgb8",
        **common,
    )
    depth_raw = tmp_path / "depth.z16"
    depth_raw.write_bytes((1000).to_bytes(2, "little") + (2000).to_bytes(2, "little"))
    depth = CameraSample(
        stream_name="wrist_depth",
        path=depth_raw,
        pixel_format="z16",
        depth_scale=0.001,
        **common,
    )

    rgb_png = tmp_path / "rgb.png"
    depth_png = tmp_path / "depth.png"
    _materialize_camera_sample(rgb, rgb_png)
    _materialize_camera_sample(depth, depth_png)

    from PIL import Image

    with Image.open(rgb_png) as image:
        assert image.mode == "RGB"
        assert list(image.getdata()) == [(255, 0, 0), (0, 255, 0)]
    with Image.open(depth_png) as image:
        assert image.mode in ("I;16", "I")
        assert list(image.getdata()) == [1000, 2000]


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
