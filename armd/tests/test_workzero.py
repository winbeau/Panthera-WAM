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
    monkeypatch.setenv("PANTHERA_WORKZERO_ENABLED", "1")
    monkeypatch.setenv("PANTHERA_TEACH_DIR", str(tmp_path / "teach"))
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=60.0,
        teach_safe_hold_s=0.6,
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


@pytest_asyncio.fixture
async def workzero_gate_off_stack(tmp_path, monkeypatch):
    """与 workzero_stack 相同但默认关闭 PANTHERA_WORKZERO_ENABLED。"""
    monkeypatch.setenv("PANTHERA_WORK_ZERO_PATH", str(tmp_path / "work-zero.json"))
    monkeypatch.setenv("PANTHERA_TEACH_DIR", str(tmp_path / "teach"))
    loop = HardwareLoop(SimBackend, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=60.0,
        teach_safe_hold_s=0.6,
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
async def test_gozero_feature_gate_and_reason_validation(workzero_gate_off_stack) -> None:
    _, stub, metadata, _, _ = workzero_gate_off_stack
    # 默认 feature gate 关闭：拒绝，不是 UNIMPLEMENTED（P3 已实现）
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(
            arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
            metadata=metadata,
        )
    assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "未启用" in exc_info.value.details()
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


def _move_sim_arm_elsewhere(backend) -> None:
    # 摆到另一组位形（与工作零位不同）
    backend._positions[:6] = np.array([0.1, 0.5, 0.6, -0.3, 0.2, -0.1], dtype=np.float64)


async def asyncio_until(predicate, timeout_s: float) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)


# ---------------------------------------------------------- P3 GoWorkZero gRPC


@pytest.mark.asyncio
async def test_gozero_rejects_without_pose(workzero_stack) -> None:
    _, stub, metadata, _, _ = workzero_stack
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(arm_pb2.GoWorkZeroRequest(confirm=True), metadata=metadata)
    assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "尚未保存" in exc_info.value.details()


@pytest.mark.asyncio
async def test_gozero_rejects_while_teach_active(workzero_stack) -> None:
    _, stub, metadata, _, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(arm_pb2.GoWorkZeroRequest(confirm=True), metadata=metadata)
    assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "运动" in exc_info.value.details()


@pytest.mark.asyncio
async def test_gozero_immediate_done_when_already_at_work_zero(workzero_stack) -> None:
    loop, stub, metadata, server, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    # 锁存当前位形（sim 初始即零位）→ 小残差路径 → 立即 DONE
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)  # 等 SAFE_HOLD 结束 + teach 清理
    assert not loop.has_active_motion

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    assert accepted.execution_id
    final = None
    fractions: list[float] = []
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        fractions.append(status.fraction)
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE
    assert fractions == sorted(fractions), "fraction 必须单调"
    assert final.fraction == pytest.approx(1.0)
    # 定死锁：不再自动切 teach（阻尼锁由录制/推理开始时显式 TeachStart 切入）
    await asyncio.sleep(1.2)
    assert server.arm_service._teach_motion is None
    # 夹爪已快速开到工作零位姿态（定死锁帧内 POS-VEL 伺服）
    cached = loop.latest_state()
    assert abs(cached.motors[6].position - saved.pose.gripper) <= 0.05
    assert accepted.execution_id


@pytest.mark.asyncio
async def test_gozero_cancel_via_execution(workzero_stack) -> None:
    loop, stub, metadata, server, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)  # 位形 A
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    assert not loop.has_active_motion
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)  # 移到位形 B（≠A）

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    await asyncio.sleep(0.3)  # 让 motion 进入 RUN
    cancelled = await stub.CancelExecution(
        arm_pb2.CancelExecutionRequest(execution_id=accepted.execution_id),
        metadata=metadata,
    )
    assert cancelled.cancelled
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_CANCELLED
    # 取消后不会自动重启
    assert not loop.has_active_motion
    # 取消/未完成：绝不自动进入 teach
    await asyncio.sleep(0.5)
    assert server.arm_service._teach_motion is None


@pytest.mark.asyncio
async def test_gozero_movel_path_reaches_work_zero_and_holds(workzero_stack) -> None:
    """大位移回位：MoveL 轨迹到工作零位 + 定死锁 + 到位误差。"""
    loop, stub, metadata, server, _ = workzero_stack

    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)  # 位形 A
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    target_joints = list(saved.pose.joints)
    target_gripper = saved.pose.gripper
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)  # 移到位形 B（≠A）
    assert not loop.has_active_motion

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE
    await asyncio.sleep(1.2)
    # 定死锁语义：无 teach；arm 到位保持 + 夹爪已开
    assert server.arm_service._teach_motion is None
    cached = loop.latest_state()
    current = [motor.position for motor in cached.motors[:6]]
    errors = [abs(c - t) for c, t in zip(current, target_joints, strict=True)]
    assert max(errors) <= 0.02, f"回位误差过大: {errors}"
    assert abs(cached.motors[6].position - target_gripper) <= 0.05


# -------------------------------------------------- P4 action-only 边界


@pytest.mark.asyncio
async def test_policy_chunk_rejected_during_zeroing(workzero_stack) -> None:
    """ZEROING/RUNNING 阶段模型 action 必须被拒（0 次 ApplyPolicyChunk 生效）。"""
    loop, stub, metadata, _, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    # 零位运动执行期间：模型策略被互斥拒绝
    response = await stub.ApplyPolicyChunk(arm_pb2.PolicyActionChunk(), metadata=metadata)
    assert not response.accepted
    assert "运动" in response.reject_reason
    # 取消后 motion 结束
    await stub.CancelExecution(
        arm_pb2.CancelExecutionRequest(execution_id=accepted.execution_id),
        metadata=metadata,
    )
    await asyncio.sleep(0.5)
    assert not loop.has_active_motion
    # 取消后 ApplyPolicyChunk 不再因运动互斥被拒（返回其它结构化原因，而非运动拒绝）
    response = await stub.ApplyPolicyChunk(arm_pb2.PolicyActionChunk(), metadata=metadata)
    assert not response.accepted
    assert "运动" not in response.reject_reason


@pytest.mark.asyncio
async def test_estop_during_gozero_no_auto_resume(workzero_stack) -> None:
    """EStop 终止 WorkZeroMotion；ClearEStop 后绝不自动恢复运动。"""
    loop, stub, metadata, _, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    await asyncio.sleep(0.3)
    await stub.EStop(arm_pb2.EStopRequest(reason="p4-test"))
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_CANCELLED
    # ClearEStop 只恢复安全阻尼，不自动重新 gozero
    await stub.ClearEStop(arm_pb2.ClearEStopRequest(confirm=True), metadata=metadata)
    await asyncio.sleep(0.5)
    assert not loop.has_active_motion


@pytest.mark.asyncio
async def test_gozero_force_acquire_cancels_execution(workzero_stack) -> None:
    """force acquire 取消在飞 gozero；新持有者不会自动恢复运动。"""
    loop, stub, metadata, _, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    await asyncio.sleep(0.3)
    # 新客户端 force acquire
    second = await stub.AcquireControl(
        arm_pb2.AcquireControlRequest(client_id="force-holder", force=True)
    )
    assert second.granted
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_CANCELLED
    await asyncio.sleep(0.5)
    assert not loop.has_active_motion, "force acquire 后不得自动恢复运动"


@pytest.mark.asyncio
async def test_rezero_full_flow_release_move_back_and_hold(workzero_stack) -> None:
    """rezero：动作完成位（teach HOLD）→ 开爪（脚本，非模型）→ 快速退出 teach
    → MoveL 回工作0位 → teach lock。"""
    loop, stub, metadata, server, _ = workzero_stack
    from armd.motion import AutoHoldState

    # 1) 位形 A（工作零位）锁存：夹爪先摆到与工作零位一致的开爪姿态
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    target_joints = list(saved.pose.joints)
    target_gripper = saved.pose.gripper
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)

    # 2) 模拟任务动作：抓取（夹爪闭合）并移到动作完成位 B，然后 lock 保持
    loop.submit(_close_gripper_sim).result(timeout=2.0)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)
    await _start_teach(stub, metadata, manual_clutch=True)
    await stub.TeachClutch(
        arm_pb2.TeachClutchRequest(mode=arm_pb2.TEACH_CLUTCH_MODE_LOCK),
        metadata=metadata,
    )
    await asyncio.sleep(0.3)
    teach = server.arm_service._teach_motion
    assert teach is not None and teach.auto_hold_state is AutoHoldState.HOLD

    # 3) rezero：开爪（松方块）→ 快速退出 teach → MoveL 回工作0位 → teach lock
    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="post_action"),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE
    await asyncio.sleep(1.2)
    # 定死锁语义：无 teach；arm 回工作0位 + 夹爪已松开
    assert server.arm_service._teach_motion is None
    cached = loop.latest_state()
    errors = [abs(motor.position - t) for motor, t in zip(cached.motors[:6], target_joints, strict=True)]
    assert max(errors) <= 0.02, f"rezero 回位误差过大: {errors}"
    # 夹爪已松开到工作零位姿态（脚本开爪，不是模型动作）
    assert abs(cached.motors[6].position - target_gripper) <= 0.05


def _close_gripper_sim(backend) -> None:
    """模拟抓取：夹爪偏离工作零位姿态（sim MIT 动力学收敛慢，偏移取小值）。"""
    backend._positions[6] = 0.03


@pytest.mark.asyncio
async def test_rezero_retry_with_active_hold_succeeds(workzero_stack) -> None:
    """rezero 中途失败后的重试：teach 已退出、定死锁 hold 仍在运行，
    重试必须能继续（此前被「已有运动正在执行」永久拒绝，真机无法恢复）。"""
    loop, stub, metadata, server, _ = workzero_stack
    import numpy as np

    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    target_gripper = saved.pose.gripper
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    # 模拟动作完成位：夹爪闭合持物
    loop.submit(_close_gripper_sim).result(timeout=2.0)
    loop.submit(_move_sim_arm_elsewhere).result(timeout=2.0)
    # 模拟第一次 rezero 中途失败后的状态：teach 已退出，hold 定死锁仍在运行
    service = server.arm_service
    cached = loop.latest_state()
    await service._start_hold_motion(
        arm_position=np.array(
            [motor.position for motor in cached.motors[:6]], dtype=np.float64
        ),
        gripper_position=target_gripper,
        gripper_velocity=0.6,
    )
    assert loop.has_active_motion
    # 重试 rezero 必须成功
    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="post_action"),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE


@pytest.mark.asyncio
async def test_rezero_rejects_without_teach_hold(workzero_stack) -> None:
    """rezero 要求 teach HOLD 状态；无 teach 时拒绝。"""
    loop, stub, metadata, _, _ = workzero_stack
    await _start_teach(stub, metadata, manual_clutch=True)
    loop.submit(_move_sim_arm).result(timeout=2.0)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    assert not loop.has_active_motion
    with pytest.raises(grpc.RpcError) as exc_info:
        await stub.GoWorkZero(
            arm_pb2.GoWorkZeroRequest(confirm=True, reason="post_action"),
            metadata=metadata,
        )
    assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "teach HOLD" in exc_info.value.details()


@pytest.mark.asyncio
async def test_gozero_hold_then_teachstart_switches_to_damping_lock(workzero_stack) -> None:
    """gozero 结束态：定死锁 hold motion 持续运行；TeachStart 自动替换为阻尼锁。"""
    loop, stub, metadata, server, _ = workzero_stack
    from armd.motion import AutoHoldState, HoldPositionMotion

    await _start_teach(stub, metadata, manual_clutch=True)
    saved = await stub.SetWorkZero(arm_pb2.SetWorkZeroRequest(confirm=True), metadata=metadata)
    assert saved.accepted
    await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    await asyncio.sleep(0.9)
    assert not loop.has_active_motion

    accepted = await stub.GoWorkZero(
        arm_pb2.GoWorkZeroRequest(confirm=True, reason="gozero"),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
        if status.state in (
            arm_pb2.EXEC_STATE_DONE,
            arm_pb2.EXEC_STATE_FAILED,
            arm_pb2.EXEC_STATE_CANCELLED,
        ):
            break
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE
    await asyncio.sleep(1.0)
    # 定死锁：hold motion 持续运行（每周期 POS-VEL 帧，看门狗安全）
    assert loop.has_active_motion
    hold = server.arm_service._hold_motion
    assert hold is not None and isinstance(hold, HoldPositionMotion)
    assert server.arm_service._teach_motion is None

    # 定死锁 → 阻尼锁：TeachStart 双锁挂接（hold 仍是活动 motion，影子阶段
    # 武装 lock 满刚度后委托 teach 写帧）。
    response = await stub.TeachStart(
        arm_pb2.TeachStartRequest(manual_clutch=True),
        metadata=metadata,
    )
    assert response.accepted, response.reject_reason
    await asyncio.sleep(0.3)
    assert server.arm_service._hold_motion is not None
    assert server.arm_service._teach_motion is not None
    # 从定死锁接管时首个 teach 周期直接进入 HOLD；不得先进入 DRAG。
    assert server.arm_service._teach_motion.auto_hold_state is AutoHoldState.HOLD
    # 双锁已交接：hold 已卸下定死锁并委托 teach 写帧（不再影子）。
    assert not server.arm_service._hold_motion.shadowing
    await stub.TeachClutch(
        arm_pb2.TeachClutchRequest(mode=arm_pb2.TEACH_CLUTCH_MODE_LOCK),
        metadata=metadata,
    )
    await asyncio.sleep(0.2)
    assert server.arm_service._teach_motion.auto_hold_state is AutoHoldState.HOLD
