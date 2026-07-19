"""panthera-cli 的 gRPC 连接与 lease 本地状态。"""

from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc, camera_pb2_grpc, dataset_pb2_grpc

LEASE_METADATA_KEY = "x-panthera-lease"
LOCAL_CHANNEL_OPTIONS = (("grpc.enable_http_proxy", 0),)


@dataclass(frozen=True, slots=True)
class SavedLease:
    endpoint: str
    client_id: str
    token: str


def endpoint() -> str:
    return os.environ.get("PANTHERA_ENDPOINT", "127.0.0.1:50051")


def camera_endpoint() -> str:
    configured = os.environ.get("PANTHERA_CAMERA_ENDPOINT")
    if configured:
        return configured
    arm_target = endpoint()
    host, separator, port = arm_target.rpartition(":")
    if separator and port.isdecimal():
        return f"{host}:50052"
    return "127.0.0.1:50052"


def default_client_id() -> str:
    return os.environ.get("PANTHERA_CLIENT_ID", f"cli@{socket.gethostname()}")


def state_file() -> Path:
    state_dir = Path(os.environ.get("PANTHERA_STATE_DIR", Path.home() / ".config" / "panthera"))
    return state_dir / "lease.json"


def save_lease(lease: SavedLease) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(lease), ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def load_lease(*, required: bool = True) -> SavedLease | None:
    path = state_file()
    if not path.is_file():
        if required:
            raise RuntimeError("本地没有控制权 lease；请先执行 panthera control acquire")
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SavedLease(endpoint=data["endpoint"], client_id=data["client_id"], token=data["token"])


def clear_lease() -> None:
    state_file().unlink(missing_ok=True)


def lease_metadata(lease: SavedLease):
    return ((LEASE_METADATA_KEY, lease.token),)


def create_stub(target: str | None = None):
    channel = grpc.insecure_channel(target or endpoint(), options=LOCAL_CHANNEL_OPTIONS)
    return channel, arm_pb2_grpc.ArmServiceStub(channel)


def create_camera_stub(target: str | None = None):
    channel = grpc.insecure_channel(target or camera_endpoint(), options=LOCAL_CHANNEL_OPTIONS)
    return channel, camera_pb2_grpc.CameraServiceStub(channel)


def create_dataset_stub(target: str | None = None):
    channel = grpc.insecure_channel(target or endpoint(), options=LOCAL_CHANNEL_OPTIONS)
    return channel, dataset_pb2_grpc.DatasetServiceStub(channel)


@contextmanager
def maintain_heartbeat(lease: SavedLease, *, interval_s: float = 0.5):
    stop = threading.Event()

    def run() -> None:
        channel, stub = create_stub(lease.endpoint)

        def requests():
            while not stop.is_set():
                yield arm_pb2.HeartbeatRequest()
                stop.wait(interval_s)

        try:
            for _ in stub.Heartbeat(requests(), metadata=lease_metadata(lease)):
                if stop.is_set():
                    break
        except grpc.RpcError:
            pass
        finally:
            channel.close()

    thread = threading.Thread(target=run, name="panthera-cli-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2.0)
