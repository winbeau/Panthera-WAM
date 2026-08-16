"""P2：工作零位存储、TeachMotion lock snapshot 与 gRPC/CLI 契约测试。"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

import grpc
import numpy as np
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import DEFAULT_LIMITS, SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.hardware_loop import HardwareLoop
from armd.motion import (
    AutoHoldConfig,
    AutoHoldState,
    TeachClutchCommand,
    TeachLockSnapshot,
    TeachMotion,
)
from armd.server import ArmdServer
from armd.workzero import (
    WORK_ZERO_SCHEMA_VERSION,
    WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK,
    WorkZeroPose,
    WorkZeroStore,
    WorkZeroValidationError,
)


@dataclass
class FakeClock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSimBackend(SimBackend):
    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.frames = []

    def write_frame(self, frame) -> None:
        self.frames.append(frame)
        super().write_frame(frame)


def make_pose(
    *,
    joints: tuple[float, ...] = (0.1, 0.2, 0.3, -0.1, -0.2, -0.3),
    gripper: float = 0.8,
    sampled_ns: int | None = 123456789,
) -> WorkZeroPose:
    return WorkZeroPose(
        schema_version=WORK_ZERO_SCHEMA_VERSION,
        joints=joints,
        gripper=gripper,
        captured_at_ms=1_700_000_000_000,
        sampled_monotonic_ns=sampled_ns,
        state_sequence=None,
        stream_instance_id="stream-1",
        source=WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK,
    )


# ---------------------------------------------------------------- Store


def test_store_round_trip(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    pose = make_pose()
    store.save(pose, DEFAULT_LIMITS)
    loaded = store.load()
    assert loaded == pose
    assert loaded.sampled_monotonic_ns == 123456789


def test_store_atomic_replace_no_leftover_tmp(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    store.save(make_pose(joints=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)), DEFAULT_LIMITS)
    store.save(make_pose(joints=(0.1, 0.2, 0.3, -0.1, -0.2, -0.3)), DEFAULT_LIMITS)
    leftovers = [name for name in os.listdir(tmp_path) if name.startswith(".work-zero.json.")]
    assert leftovers == []
    assert store.load().joints == (0.1, 0.2, 0.3, -0.1, -0.2, -0.3)


def test_store_file_mode_0600(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    store.save(make_pose(), DEFAULT_LIMITS)
    mode = stat.S_IMODE((tmp_path / "work-zero.json").stat().st_mode)
    assert mode == 0o600


def test_store_rejects_malformed_json(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    (tmp_path / "work-zero.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(WorkZeroValidationError):
        store.load()


def test_store_rejects_nan_infinity_literals(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    pose = make_pose()
    raw = json.dumps(pose.to_dict())
    (tmp_path / "work-zero.json").write_text(raw.replace("0.3", "NaN"), encoding="utf-8")
    with pytest.raises(WorkZeroValidationError):
        store.load()


def test_store_rejects_unknown_schema(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    pose = make_pose()
    data = pose.to_dict()
    data["schema_version"] = 99
    (tmp_path / "work-zero.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkZeroValidationError, match="schema_version"):
        store.load()


def test_store_rejects_wrong_dimension(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    pose = make_pose()
    data = pose.to_dict()
    data["joints"] = [0.0, 0.0, 0.0]  # 只有 3 个
    (tmp_path / "work-zero.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkZeroValidationError, match="joints"):
        store.load()


def test_store_rejects_missing_fields(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    pose = make_pose()
    data = pose.to_dict()
    del data["source"]
    (tmp_path / "work-zero.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkZeroValidationError, match="source"):
        store.load()


def test_store_rejects_out_of_soft_limits_on_save(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    # J2 下限 -0.1：越限保存必须拒绝且不写文件
    with pytest.raises(WorkZeroValidationError, match="joint2"):
        store.save(make_pose(joints=(0.0, -2.0, 0.0, 0.0, 0.0, 0.0)), DEFAULT_LIMITS)
    assert not (tmp_path / "work-zero.json").exists()
    with pytest.raises(WorkZeroValidationError, match="gripper"):
        store.save(make_pose(gripper=2.5), DEFAULT_LIMITS)
    assert not (tmp_path / "work-zero.json").exists()


def test_store_missing_file_returns_none(tmp_path: Path) -> None:
    assert WorkZeroStore(tmp_path / "nope.json").load() is None


def test_store_rejects_directory(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    (tmp_path / "work-zero.json").mkdir()
    with pytest.raises(WorkZeroValidationError, match="普通文件"):
        store.load()


def test_store_concurrent_saves_never_leave_partial_json(tmp_path: Path) -> None:
    store = WorkZeroStore(tmp_path / "work-zero.json")
    poses = [
        make_pose(joints=tuple(float(i) * 0.01 + j * 0.001 for i in range(6)), gripper=0.1 + j * 0.1)
        for j in range(8)
    ]
    errors: list[BaseException] = []

    def worker(pose: WorkZeroPose) -> None:
        try:
            for _ in range(10):
                store.save(pose, DEFAULT_LIMITS)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(pose,)) for pose in poses]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    # 文件始终是完整 JSON 且能加载
    loaded = store.load()
    assert loaded is not None
    assert loaded.gripper in {0.1 + j * 0.1 for j in range(8)}


# ------------------------------------------------- TeachMotion lock snapshot


def test_teach_lock_generation_advances_only_on_explicit_lock() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    backend._positions[6] = 1.25
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(),
        manual_clutch=True,
    )
    assert motion.lock_generation == 0
    assert motion.lock_snapshot is None

    # 自动模式（manual_clutch=False 才会走 STILL_DETECT；显式离合模式不做自动锁位）
    auto = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(
            still_velocity_threshold=0.5,
            still_duration=0.1,
            release_velocity_threshold=0.9,
        ),
        manual_clutch=False,
    )
    clock.advance(0.5)
    auto.step(backend, clock.now)
    clock.advance(0.5)
    auto.step(backend, clock.now)
    assert auto.lock_generation == 0, "自动 HOLD 不得增加 lock generation"

    # 显式 lock：下一周期消费
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)
    assert motion.lock_generation == 1
    snapshot = motion.lock_snapshot
    assert isinstance(snapshot, TeachLockSnapshot)
    assert snapshot.generation == 1
    assert snapshot.state is AutoHoldState.HOLD
    states = backend.read_all()
    assert snapshot.joints == tuple(float(state.position) for state in states[:6])
    assert snapshot.gripper == pytest.approx(states[6].position)
    assert len(snapshot.joints) == 6
    assert snapshot.captured_monotonic_ns == pytest.approx(clock.now * 1e9, rel=1e-9)

    # 再次 lock：generation 递增
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)
    assert motion.lock_generation == 2
    assert motion.lock_snapshot.generation == 2


def test_teach_lock_snapshot_uses_same_read_all_sample() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    backend._positions[6] = 0.42
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(),
        manual_clutch=True,
    )
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)
    snapshot = motion.lock_snapshot
    assert snapshot is not None
    # 夹爪与关节来自同一个 read_all 样本：与后端当前读数一致
    assert snapshot.gripper == pytest.approx(0.42)
    assert snapshot.joints == tuple(float(state.position) for state in backend.read_all()[:6])


def test_teach_drag_keeps_persisted_snapshot_but_releases_hold() -> None:
    clock = FakeClock()
    backend = RecordingSimBackend(clock=clock)
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(),
        manual_clutch=True,
    )
    motion.request_clutch(TeachClutchCommand.LOCK)
    motion.step(backend, clock.now)
    generation_after_lock = motion.lock_generation
    snapshot_after_lock = motion.lock_snapshot
    motion.request_clutch(TeachClutchCommand.DRAG)
    motion.step(backend, clock.now)  # 消费 DRAG → RELEASE
    clock.advance(0.5)
    motion.step(backend, clock.now)  # RELEASE ramp 完成 → DRAG
    assert motion.auto_hold_state is AutoHoldState.DRAG
    assert motion.lock_generation == generation_after_lock
    assert motion.lock_snapshot == snapshot_after_lock, "drag 不得清除已捕获的 lock snapshot"


def test_teach_request_clutch_requires_manual_clutch() -> None:
    motion = TeachMotion(
        kp=np.zeros(6),
        kd=np.zeros(6),
        fc=np.zeros(6),
        fv=np.zeros(6),
        auto_hold=AutoHoldConfig(enabled=False),
        manual_clutch=False,
    )
    with pytest.raises(ValueError, match="clutch"):
        motion.request_clutch(TeachClutchCommand.LOCK)


# ---------------------------------------------------------------- gRPC


@pytest_asyncio.fixture
async def workzero_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHERA_WORK_ZERO_PATH", str(tmp_path / "work-zero.json"))
    monkeypatch.setenv("PANTHERA_TEACH_DIR", str(tmp_path / "teach"))
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=60.0,
        teach_safe_hold_s=2.0,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="workzero-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata, server, tmp_path
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


async def _start_teach(stub, metadata, *, manual_clutch: bool = True) -> None:
    response = await stub.TeachStart(
        arm_pb2.TeachStartRequest(manual_clutch=manual_clutch),
        metadata=metadata,
    )
    assert response.accepted, response.reject_reason


@pytest.mark.asyncio
async def test_workzero_requires_lease(workzero_stack) -> None:
    _, stub, _, _, _ = workzero_stack
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True))
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(arm_pb2.GoWorkZeroRequest(confirm=True))
    assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_workzero_confirm_is_server_enforced(workzero_stack) -> None:
    _, stub, metadata, _, _ = workzero_stack
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=False), metadata=metadata)
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(arm_pb2.GoWorkZeroRequest(confirm=False), metadata=metadata)
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_setworkzero_rejects_without_teach(workzero_stack) -> None:
    _, stub, metadata, _, tmp_path = workzero_stack
    response = await stub.SetWorkZero(
        arm_pb2.SetWorkZeroRequest(confirm=True, lock_wait_timeout_s=0.5),
        metadata=metadata,
    )
    assert not response.accepted
    assert "teach" in response.reject_reason
    assert not (tmp_path / "work-zero.json").exists()


@pytest.mark.asyncio
async def test_setworkzero_rejects_without_manual_clutch(workzero_stack) -> None:
    _, stub, metadata, _, tmp_path = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=False)
    response = await stub.SetWorkZero(
        arm_pb2.SetWorkZeroRequest(confirm=True, lock_wait_timeout_s=0.5),
        metadata=metadata,
    )
    assert not response.accepted
    assert "clutch" in response.reject_reason
    assert not (tmp_path / "work-zero.json").exists()


@pytest.mark.asyncio
async def test_setworkzero_lock_timeout_rejects_without_write(workzero_stack) -> None:
    _, stub, metadata, _, tmp_path = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    # 先取消 teach：进入 SAFE_HOLD 后 LOCK 命令被忽略，generation 不前进 → 超时
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    response = await stub.SetWorkZero(
        arm_pb2.SetWorkZeroRequest(confirm=True, lock_wait_timeout_s=0.3),
        metadata=metadata,
    )
    assert not response.accepted
    assert "超时" in response.reject_reason
    assert not (tmp_path / "work-zero.json").exists()


@pytest.mark.asyncio
async def test_setworkzero_full_flow_and_drag_keeps_file(workzero_stack) -> None:
    loop, stub, metadata, _, tmp_path = workzero_stack
    # 初始无工作零位
    empty = await stub.GetWorkZero(arm_pb2.Empty())
    assert not empty.exists

    await _start_teach(stub, metadata, manual_clutch=True)
    # 摆一个明显非零位形再锁存
    loop.submit(_move_sim_arm).result(timeout=2.0)
    response = await stub.SetWorkZero(
        arm_pb2.SetWorkZeroRequest(confirm=True, lock_wait_timeout_s=2.0),
        metadata=metadata,
    )
    assert response.accepted, response.reject_reason
    assert response.saved
    assert response.lock_generation == 1
    pose = response.pose
    assert pose.schema_version == WORK_ZERO_SCHEMA_VERSION
    assert len(pose.joints) == 6
    assert pose.source == WORK_ZERO_SOURCE_TEACH_CLUTCH_LOCK
    assert pose.HasField("sampled_monotonic_ns")

    # GetWorkZero 可读且 7 轴有限、无 reject
    got = await stub.GetWorkZero(arm_pb2.Empty())
    assert got.exists
    assert got.reject_reason == ""
    assert len(got.pose.joints) == 6
    assert np.isfinite(got.pose.gripper)

    # drag 后文件不变（快照仍是锁存值）
    await stub.TeachClutch(
        arm_pb2.TeachClutchRequest(mode=arm_pb2.TEACH_CLUTCH_MODE_DRAG),
        metadata=metadata,
    )
    await asyncio.sleep(0.2)
    again = await stub.GetWorkZero(arm_pb2.Empty())
    assert again.exists
    assert again.pose.joints == pose.joints
    assert again.pose.gripper == pose.gripper
    # 文件权限 0600
    assert stat.S_IMODE((tmp_path / "work-zero.json").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_gozero_unimplemented_until_p3(workzero_stack) -> None:
    _, stub, metadata, _, _ = workzero_stack
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(
            arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
            metadata=metadata,
        )
    assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(
            arm_pb2.GoWorkZeroRequest(confirm=True, reason="bad"),
            metadata=metadata,
        )
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_setzero_calibrate_path_unchanged(workzero_stack) -> None:
    _, stub, metadata, _, _ = workzero_stack
    # calibrate zero 仍是独立路径：缺 confirm 拒绝，与 workzero 无关
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.SetZero(arm_pb2.SetZeroRequest(confirm=False), metadata=metadata)
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    accepted = await stub.SetZero(arm_pb2.SetZeroRequest(confirm=True), metadata=metadata)
    assert accepted.accepted


def _move_sim_arm(backend) -> None:
    # 在 HardwareLoop 线程内把仿真臂摆到非零位形（仅测试夹具）
    backend._positions[:6] = np.array([0.2, 0.3, 0.4, -0.2, 0.1, -0.3], dtype=np.float64)


async def asyncio_until(predicate, timeout_s: float) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
