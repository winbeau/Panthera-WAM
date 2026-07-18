#!/usr/bin/env python3
"""M0-2：armd 自建 moveL 执行循环（**会让机械臂运动**）。

验证 FINAL_PLAN §1.2 / §4b：不调用 SDK 的 `moveL()`（它内部 `_execute_trajectory`
会把线程钉死整条轨迹、无取消缝隙、无进度），改用公开原语在自己的循环里逐点步进，
从而获得：单调递增的 `fraction`、随时可取消、每点可查 estop。

自建循环严格复刻 `Panthera._execute_trajectory`（`Panthera.py:1321-1379`）：
    tqe = clip(get_Gravity(q_i), ±max_tqu)
    pos_vel_tqe_kp_kd(q_i, v_i, tqe, kp=[30,50,60,25,15,10], kd=[3,5,6,2.5,1.5,1])
差别只在「等待到点」的方式：SDK 忙等钉死，我们逐周期让出并检查 estop/cancel。

对拍口径：同一目标，先用自建循环走一次记录末端误差，再（可选）用 SDK 原版
`moveL()` 走一次，比较两者末端误差是否一致 → 证明「等价重写」成立。

N7 注意：本脚本只写 6 个关节槽位。切到 MODE_POS_VEL_TQE_KP_KD_2 时夹爪槽位
会被清成 0x8000（无指令），这是安全的；但 armd 正式实现必须每周期把夹爪也
按同一模式写满（见 FINAL_PLAN §V6-N7）。

⚠ 必须在 M0-1 通过之后、操作者在场时运行。

用法：
    python spikes/m0/m0_2_movel_loop.py --axis z --delta 0.03 --duration 4
    python spikes/m0/m0_2_movel_loop.py --axis z --delta 0.03 --cancel-at 0.5
    python spikes/m0/m0_2_movel_loop.py --axis z --delta 0.03 --compare-sdk
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from m0_common import confirm_motion, load_config, sdk_paths

SAFE_MAX_TORQUE = np.array([10.0, 18.0, 18.0, 10.0, 5.0, 5.0])
KP = [30.0, 50.0, 60.0, 25.0, 15.0, 10.0]   # 与 _execute_trajectory 一致
KD = [3.0, 5.0, 6.0, 2.5, 1.5, 1.0]
AXIS = {"x": 0, "y": 1, "z": 2}


def execute_trajectory_stepwise(robot, traj, timestamps, vels, max_tqu, *, cancel_at=None):
    """自建执行循环：逐点下发 + 每点发布 fraction + 每点可取消。"""
    n = len(traj)
    fractions: list[float] = []
    cancelled = False
    t0 = time.perf_counter()

    for i in range(n):
        frac = (i + 1) / n

        # ---- 每点先查取消（对应 §1.1 步骤2）----
        if cancel_at is not None and frac >= cancel_at:
            print(f"\n  [cancel] fraction={frac:.3f} 触发取消 → 安全收尾（沿剩余轨迹减速）")
            q_now = np.asarray(traj[i])
            for k in range(12):                      # 12 步线性把速度前馈降到 0
                scale = 1.0 - (k + 1) / 12.0
                tqe = np.clip(np.asarray(robot.get_Gravity(q_now)), -max_tqu, max_tqu)
                robot.pos_vel_tqe_kp_kd(q_now, np.asarray(vels[i]) * scale, tqe, KP, KD)
                time.sleep(0.01)
            cancelled = True
            break

        # ---- 等待到该点的时间戳（可让出，不钉死）----
        while (time.perf_counter() - t0) < timestamps[i]:
            time.sleep(0.0002)

        q_i = np.asarray(traj[i])
        tqe = np.clip(np.asarray(robot.get_Gravity(q_i)), -max_tqu, max_tqu)
        ok = robot.pos_vel_tqe_kp_kd(q_i, np.asarray(vels[i]), tqe, KP, KD)
        if ok is False:
            print(f"  ✗ 第 {i+1}/{n} 点被拒绝（限位？）")
            return fractions, False, True

        fractions.append(frac)
        if (i + 1) % max(1, n // 10) == 0:
            print(f"    fraction={frac:.3f}  t={timestamps[i]:.2f}s")

    return fractions, not cancelled, cancelled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", choices=list(AXIS), default="z")
    ap.add_argument("--delta", type=float, default=0.03, help="直线位移(m)，默认3cm")
    ap.add_argument("--duration", type=float, default=4.0)
    ap.add_argument("--cancel-at", type=float, default=None, help="在该 fraction 处测试取消")
    ap.add_argument("--compare-sdk", action="store_true", help="额外用 SDK 原版 moveL 走一次对拍")
    ap.add_argument("--config", default="Follower.yaml")
    args = ap.parse_args()

    if abs(args.delta) > 0.10:
        raise SystemExit("安全限制：本 spike 位移不超过 10cm")

    scripts_dir, param_dir = sdk_paths()
    sys.path.insert(0, str(scripts_dir))
    from Panthera_lib import Panthera  # noqa: E402

    config, _ = load_config(args.config)

    print("=" * 72)
    print("M0-2  自建 moveL 执行循环（不调用 SDK moveL）")
    print("=" * 72)
    robot = Panthera(str(param_dir / args.config))
    robot.send_get_motor_state_cmd()
    robot.motor_send_cmd()
    time.sleep(0.2)

    q0 = robot.get_current_pos()
    fk0 = robot.forward_kinematics(q0)
    p0 = np.array(fk0["position"])
    target = p0.copy()
    target[AXIS[args.axis]] += args.delta

    print(f"\n当前末端位置: {np.round(p0, 4)}")
    print(f"目标末端位置: {np.round(target, 4)}  （{args.axis}{args.delta:+.3f} m）")

    # ---- 规划（纯计算，不动）----
    print("\n[规划] compute_cartesian_path → time_parameterization → spline")
    start_pose = {"position": fk0["position"], "rotation": fk0["rotation"]}
    end_pose = {"position": target.tolist(), "rotation": fk0["rotation"]}
    traj, fraction = robot.compute_cartesian_path([start_pose, end_pose])
    if not traj:
        raise SystemExit("路径规划失败，未发送任何指令")
    print(f"  规划点数={len(traj)}  fraction={fraction:.3f}")
    if fraction < 0.99:
        raise SystemExit(f"规划只完成 {fraction*100:.1f}%，为安全起见中止，未发送任何指令")

    timestamps = robot.compute_time_parameterization(traj, args.duration)
    traj, timestamps, vels = robot.smooth_trajectory_spline(traj, timestamps)
    print(f"  样条重采样后点数={len(traj)}  总时长={timestamps[-1]:.2f}s")

    actions = [
        f"末端沿 {args.axis} 轴直线移动 {args.delta*100:+.1f} cm，耗时 {args.duration}s",
        f"自建循环逐点下发 pos_vel_tqe_kp_kd（{len(traj)} 个点），kp={KP} kd={KD}",
        f"力矩上限（保守）: {SAFE_MAX_TORQUE.tolist()}",
    ]
    if args.cancel_at:
        actions.append(f"将在 fraction={args.cancel_at} 处测试取消并减速收尾")
    if args.compare_sdk:
        actions.append("随后回到起点，再用 SDK 原版 moveL() 走同一条线做对拍（第二次运动）")
    confirm_motion("笛卡尔直线运动（自建执行循环）", actions)

    print("\n[执行] 自建循环...\n")
    try:
        fracs, done, cancelled = execute_trajectory_stepwise(
            robot, traj, timestamps, vels, SAFE_MAX_TORQUE, cancel_at=args.cancel_at
        )
    finally:
        robot.set_stop()
        print("\n[finally] 已 set_stop()")

    time.sleep(0.5)
    robot.send_get_motor_state_cmd()
    robot.motor_send_cmd()
    time.sleep(0.2)
    p_custom = np.array(robot.forward_kinematics(robot.get_current_pos())["position"])
    err_custom = float(np.linalg.norm(p_custom - target))

    monotonic = all(b > a for a, b in zip(fracs, fracs[1:]))
    print("\n" + "=" * 72)
    print("结果")
    print("=" * 72)
    print(f"  fraction 点数={len(fracs)}  单调递增={monotonic} {'✅' if monotonic else '❌'}")
    print(f"  终态: {'CANCELLED' if cancelled else ('DONE' if done else 'FAILED')}")
    print(f"  自建循环末端误差: {err_custom*1000:.2f} mm  (末端 {np.round(p_custom,4)})")

    if args.compare_sdk and not cancelled:
        print("\n[对拍] 回到起点后调用 SDK 原版 moveL()...")
        robot.moveJ(q0, duration=args.duration, max_tqu=SAFE_MAX_TORQUE, iswait=True, timeout=20.0)
        time.sleep(0.5)
        robot.moveL(target.tolist(), target_rotation=fk0["rotation"],
                    duration=args.duration, use_spline=True, max_tqu=SAFE_MAX_TORQUE)
        robot.set_stop()
        time.sleep(0.5)
        robot.send_get_motor_state_cmd()
        robot.motor_send_cmd()
        time.sleep(0.2)
        p_sdk = np.array(robot.forward_kinematics(robot.get_current_pos())["position"])
        err_sdk = float(np.linalg.norm(p_sdk - target))
        diff = float(np.linalg.norm(p_custom - p_sdk))
        print(f"  SDK moveL 末端误差 : {err_sdk*1000:.2f} mm")
        print(f"  两者末端差异       : {diff*1000:.2f} mm")
        print(f"  → 等价重写{'成立 ✅' if abs(err_custom - err_sdk) < 0.005 else '存疑 ❌（差异 >5mm，需排查）'}")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
