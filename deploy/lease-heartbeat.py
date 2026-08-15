#!/usr/bin/env python3
"""Acquire and maintain the Panthera control lease without uv startup latency."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc

ENDPOINT = os.environ.get("PANTHERA_LOCAL_BIND", "127.0.0.1:50051")
CLIENT_ID = os.environ.get("PANTHERA_CLIENT_ID", "teach-cal")
LEASE_PATH = Path.home() / ".config" / "panthera" / "lease.json"
_stop = False


def stop_handler(_signum: int, _frame: object) -> None:
    global _stop
    _stop = True


def main() -> int:
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    channel = grpc.insecure_channel(ENDPOINT)
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    response = stub.AcquireControl(
        arm_pb2.AcquireControlRequest(client_id=CLIENT_ID),
        timeout=1.0,
    )
    if not response.granted:
        raise SystemExit(f"control lease denied: {response.holder_client_id}")
    LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEASE_PATH.write_text(
        json.dumps(
            {
                "endpoint": ENDPOINT,
                "client_id": CLIENT_ID,
                "token": response.lease_token,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    LEASE_PATH.chmod(0o600)
    metadata = (("x-panthera-lease", response.lease_token),)
    print(f"lease acquired: {CLIENT_ID} endpoint={ENDPOINT}", flush=True)
    try:
        while not _stop:
            stub.HeartbeatOnce(
                arm_pb2.HeartbeatRequest(),
                metadata=metadata,
                timeout=1.0,
            )
            time.sleep(0.5)
    finally:
        try:
            stub.ReleaseControl(arm_pb2.Empty(), metadata=metadata, timeout=1.0)
        except grpc.RpcError:
            pass
        channel.close()
        print("lease released", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
