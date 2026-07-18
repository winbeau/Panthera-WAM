"""真机 MoveL 取消验收：相对 Z 位移，达到指定 fraction 后安全取消。"""

from __future__ import annotations

import argparse
import json
import socket

import grpc
from panthera_arm import arm_pb2, arm_pb2_grpc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="127.0.0.1:50051")
    parser.add_argument("--delta-z-cm", type=float, required=True)
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--cancel-fraction", type=float, default=0.5)
    parser.add_argument("--confirm", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.confirm != "YES":
        raise SystemExit("拒绝执行：必须提供 --confirm YES")
    if not 0 < abs(args.delta_z_cm) <= 3.0:
        raise SystemExit("拒绝执行：|delta-z-cm| 必须位于 (0, 3]")
    if args.duration_s < 2.0:
        raise SystemExit("拒绝执行：duration-s 不得小于 2 秒")
    if not 0.1 <= args.cancel_fraction <= 0.8:
        raise SystemExit("拒绝执行：cancel-fraction 必须位于 [0.1, 0.8]")

    channel = grpc.insecure_channel(args.endpoint, options=(("grpc.enable_http_proxy", 0),))
    stub = arm_pb2_grpc.ArmServiceStub(channel)
    lease_metadata: tuple[tuple[str, str], ...] | None = None
    execution_id = ""
    statuses: list[dict[str, object]] = []
    cancelled = False
    before_position: list[float] = []
    after_position: list[float] = []
    try:
        control = stub.GetControlStatus(arm_pb2.Empty(), timeout=5)
        if control.held or control.estop_engaged:
            raise RuntimeError(f"控制前置条件不满足: held={control.held}, estop={control.estop_engaged}")

        before = stub.GetForwardKinematics(arm_pb2.JointAnglesOptional(), timeout=10)
        before_position = list(before.position)
        target_position = list(before.position)
        target_position[2] += args.delta_z_cm / 100.0
        target = arm_pb2.CartesianPose(
            position=target_position,
            matrix=arm_pb2.RotationMatrix(values=before.rotation_matrix),
        )
        preview = stub.PlanCartesianPath(
            arm_pb2.PlanCartesianPathRequest(waypoints=[target]),
            timeout=15,
        )
        if preview.fraction < 0.999 or not preview.joint_trajectory:
            raise RuntimeError(f"路径预览失败: fraction={preview.fraction:.6f}")

        print(
            json.dumps(
                {
                    "action": "MoveL then cancel",
                    "endpoint": args.endpoint,
                    "before_xyz_m": before_position,
                    "target_xyz_m": target_position,
                    "delta_z_cm": args.delta_z_cm,
                    "duration_s": args.duration_s,
                    "cancel_fraction": args.cancel_fraction,
                    "preview_fraction": preview.fraction,
                    "preview_points": len(preview.joint_trajectory),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )

        acquired = stub.AcquireControl(
            arm_pb2.AcquireControlRequest(client_id=f"v1-movel-cancel@{socket.gethostname()}"),
            timeout=5,
        )
        if not acquired.granted:
            raise RuntimeError(f"获取控制权失败，当前持有者: {acquired.holder_client_id}")
        lease_metadata = (("x-panthera-lease", acquired.lease_token),)

        accepted = stub.MoveL(
            arm_pb2.MoveLRequest(target=target, duration_s=args.duration_s),
            metadata=lease_metadata,
            timeout=20,
        )
        execution_id = accepted.execution_id
        last_fraction = -1.0
        final = None
        for status in stub.StreamExecution(arm_pb2.StreamExecutionRequest(execution_id=execution_id)):
            stub.HeartbeatOnce(
                arm_pb2.HeartbeatRequest(),
                metadata=lease_metadata,
                timeout=2,
            )
            if status.fraction + 1e-9 < last_fraction:
                raise RuntimeError(f"fraction 非单调: {last_fraction:.6f} -> {status.fraction:.6f}")
            last_fraction = status.fraction
            item = {
                "state": arm_pb2.ExecState.Name(status.state),
                "fraction": status.fraction,
                "error": status.error_message,
            }
            statuses.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if (
                not cancelled
                and status.state == arm_pb2.EXEC_STATE_RUNNING
                and status.fraction >= args.cancel_fraction
            ):
                response = stub.CancelExecution(
                    arm_pb2.CancelExecutionRequest(execution_id=execution_id),
                    metadata=lease_metadata,
                    timeout=5,
                )
                if not response.cancelled:
                    raise RuntimeError("CancelExecution 未接受取消请求")
                cancelled = True
                print(
                    json.dumps(
                        {"cancel_requested_at_fraction": status.fraction},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            final = status

        after = stub.GetForwardKinematics(arm_pb2.JointAnglesOptional(), timeout=10)
        after_position = list(after.position)
        if final is None or final.state != arm_pb2.EXEC_STATE_CANCELLED:
            final_name = "NONE" if final is None else arm_pb2.ExecState.Name(final.state)
            raise RuntimeError(f"取消验收终态错误: {final_name}")
        if not cancelled:
            raise RuntimeError("执行流结束前未触发取消")
    finally:
        if lease_metadata is not None:
            try:
                stub.ReleaseControl(arm_pb2.Empty(), metadata=lease_metadata, timeout=5)
            except grpc.RpcError as exc:
                print(f"release warning: {exc.code().name}: {exc.details()}", flush=True)
        channel.close()

    print(
        json.dumps(
            {
                "execution_id": execution_id,
                "terminal_state": statuses[-1]["state"],
                "terminal_fraction": statuses[-1]["fraction"],
                "before_xyz_m": before_position,
                "after_xyz_m": after_position,
                "actual_delta_z_cm": (after_position[2] - before_position[2]) * 100.0,
                "samples": len(statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
