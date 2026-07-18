"""panthera-cli 的 gRPC 连接与 lease 本地状态。"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

import grpc
from panthera_arm import arm_pb2_grpc

LEASE_METADATA_KEY = "x-panthera-lease"


@dataclass(frozen=True, slots=True)
class SavedLease:
    endpoint: str
    client_id: str
    token: str


def endpoint() -> str:
    return os.environ.get("PANTHERA_ENDPOINT", "127.0.0.1:50051")


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
    channel = grpc.insecure_channel(target or endpoint())
    return channel, arm_pb2_grpc.ArmServiceStub(channel)
