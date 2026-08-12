"""真实 MIT teach/playback 坐标契约安全门控的回归测试。

门控语义（ArmService.allow_unverified_teach）：
    None = 自动（仿真放行 / 真机拒绝）；False = 一律拒绝；True = 一律放行。
    门控只作用于 MIT teach/playback；POS-VEL 回放不使用重力前馈，不受门控。
测试全部使用 SimBackend 及其 is_sim=False 子类（FakeRealBackend）：
不触碰真实硬件即可覆盖「真机默认拒绝」与「显式放行」两条路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import grpc
import pytest
import pytest_asyncio
from panthera_arm import arm_pb2, arm_pb2_grpc

from armd.backend import SimBackend
from armd.control import LEASE_METADATA_KEY
from armd.grpc_service import TEACH_CONTRACT_GATE_REASON
from armd.hardware_loop import HardwareLoop
from armd.server import ArmdServer


class FakeRealBackend(SimBackend):
    """is_sim=False 的仿真后端，用于在离线环境覆盖真机门控路径。"""

    is_sim = False


async def _make_server(*, backend_factory, allow_unverified_teach: bool | None, tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHERA_TEACH_DIR", str(tmp_path / "teach"))
    loop = HardwareLoop(backend_factory, control_hz=200.0)
    loop.start()
    server = ArmdServer(
        loop,
        bind="127.0.0.1:0",
        lease_timeout_s=60.0,
        allow_unverified_teach=allow_unverified_teach,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{server.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    await channel.channel_ready()
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    acquired = await stub.AcquireControl(arm_pb2.AcquireControlRequest(client_id="gate-test"))
    metadata = ((LEASE_METADATA_KEY, acquired.lease_token),)
    try:
        yield loop, stub, metadata, server
    finally:
        await channel.close()
        await server.stop()
        loop.stop()


@pytest_asyncio.fixture
async def gated_server(tmp_path, monkeypatch):
    async for value in _make_server(
        backend_factory=SimBackend,
        allow_unverified_teach=False,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    ):
        yield value


@pytest_asyncio.fixture
async def real_auto_server(tmp_path, monkeypatch):
    async for value in _make_server(
        backend_factory=FakeRealBackend,
        allow_unverified_teach=None,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    ):
        yield value


@pytest_asyncio.fixture
async def real_allowed_server(tmp_path, monkeypatch):
    async for value in _make_server(
        backend_factory=FakeRealBackend,
        allow_unverified_teach=True,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    ):
        yield value


@pytest_asyncio.fixture
async def auto_server(tmp_path, monkeypatch):
    async for value in _make_server(
        backend_factory=SimBackend,
        allow_unverified_teach=None,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    ):
        yield value


def _write_teach_file(server: ArmdServer, path: str) -> Path:
    root = server.arm_service._teach_store.root
    file_path = root / path
    file_path.write_text(
        "\n".join(
            [
                json.dumps({"t": 0.0, "pos": [0.0] * 6, "vel": [0.0] * 6, "gripper_pos": 0.0, "gripper_vel": 0.0}),
                json.dumps({"t": 0.4, "pos": [0.01] * 6, "vel": [0.02] * 6, "gripper_pos": 0.01, "gripper_vel": 0.02}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return file_path


@pytest.mark.asyncio
async def test_auto_gate_rejects_teach_start_on_real_backend(real_auto_server) -> None:
    loop, stub, metadata, _ = real_auto_server
    response = await stub.TeachStart(arm_pb2.TeachStartRequest(), metadata=metadata)
    assert not response.accepted
    assert TEACH_CONTRACT_GATE_REASON in response.reject_reason
    # 拒绝发生在运动启动前：未产生任何活动运动。
    assert not loop.has_active_motion


@pytest.mark.asyncio
async def test_gate_rejects_mit_playback_but_allows_posvel(gated_server) -> None:
    _, stub, metadata, server = gated_server
    path = _write_teach_file(server, "gate.jsonl")

    with pytest.raises(grpc.aio.AioRpcError) as mit_error:
        await stub.TeachPlay(
            arm_pb2.TeachPlayRequest(
                path=str(path),
                mode=arm_pb2.PLAYBACK_MODE_MIT,
                kp=[1.0] * 6,
                kd=[0.1] * 6,
                playback_dt=0.01,
                smooth_window=1,
            ),
            metadata=metadata,
        )
    assert mit_error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert TEACH_CONTRACT_GATE_REASON in mit_error.value.details()

    # POS-VEL 回放不使用重力前馈，不受 MIT 门控影响。
    accepted = await stub.TeachPlay(
        arm_pb2.TeachPlayRequest(
            path=str(path),
            mode=arm_pb2.PLAYBACK_MODE_POSVEL,
            playback_dt=0.01,
            smooth_window=1,
        ),
        metadata=metadata,
    )
    final = None
    async for status in stub.StreamExecution(
        arm_pb2.StreamExecutionRequest(execution_id=accepted.execution_id)
    ):
        final = status
    assert final is not None and final.state == arm_pb2.EXEC_STATE_DONE


@pytest.mark.asyncio
async def test_explicit_allow_releases_real_teach_start(real_allowed_server) -> None:
    _, stub, metadata, _ = real_allowed_server
    started = await stub.TeachStart(arm_pb2.TeachStartRequest(), metadata=metadata)
    assert started.accepted
    stopped = await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    assert stopped.accepted


@pytest.mark.asyncio
async def test_auto_gate_keeps_sim_teach_lifecycle_working(auto_server) -> None:
    _, stub, metadata, _ = auto_server
    started = await stub.TeachStart(arm_pb2.TeachStartRequest(), metadata=metadata)
    assert started.accepted
    stopped = await stub.TeachStop(arm_pb2.Empty(), metadata=metadata)
    assert stopped.accepted
