#!/usr/bin/env python3
"""M0-1：非阻塞 HardwareLoop + 可抢占 EStop（**会让机械臂运动**）。

验证 FINAL_PLAN §1.1/§1.2/§1.3 的核心主张：
  只要 HardwareLoop 永不长阻塞，asyncio 侧置一个 estop 标志后，
  循环就能在**下一个控制周期开头**读到并执行 set_stop()，总延迟 << 100ms。

对照的是被废弃的做法：`Joint_Pos_Vel(iswait=True)` 会把循环钉死在
`wait_for_position` 的 Python 轮询里（`Panthera.py:591-599`），期间无法抢占。

安全设计：
  - 单关节、小角度（默认 ≤5°），符合 CLAUDE.md 首次真机顺序
  - 保守力矩上限
  - 运动前打印全部动作并要求输入 YES
  - finally 中无条件 set_stop()

⚠ 必须在操作者在场、手可及急停的情况下运行。

用法（wsl-host，机械臂已 usbipd 挂载）：
    source ~/panthera-wam-env/bin/activate
    python spikes/m0/m0_1_estop_preempt.py --joint 1 --deg 5 --hz 200
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import numpy as np

from m0_common import confirm_motion, load_config, percentiles, sdk_paths

# 保守力矩上限（配置默认为 [21,36,36,21,10,10]，此处取约一半）
SAFE_MAX_TORQUE = [10.0, 18.0, 18.0, 10.0, 5.0, 5.0]


def septic(start, end, duration, t):
    """与 SDK `Panthera.septic_interpolation` 同式的七次插值（位置分量）。"""
    start, end = np.asarray(start, float), np.asarray(end, float)
    if t <= 0:
        return start, np.zeros_like(start)
    if t >= duration:
        return end, np.zeros_like(end)
    s = t / duration
    a1 = 35 * s**4 - 84 * s**5 + 70 * s**6 - 20 * s**7
    da1 = (140 * s**3 - 420 * s**4 + 420 * s**5 - 140 * s**6) / duration
    return start + (end - start) * a1, (end - start) * da1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--joint", type=int, default=1, help="要动的关节号 1-6")
    ap.add_argument("--deg", type=float, default=5.0, help="运动幅度（度），默认5°")
    ap.add_argument("--hz", type=float, default=200.0, help="控制循环目标频率")
    ap.add_argument("--duration", type=float, default=4.0, help="整段运动时长(s)")
    ap.add_argument("--estop-at", type=float, default=1.5, help="第几秒触发 estop")
    ap.add_argument("--config", default="Follower.yaml")
    args = ap.parse_args()

    if not (1 <= args.joint <= 6):
        raise SystemExit("--joint 必须在 1..6")
    if abs(args.deg) > 15.0:
        raise SystemExit("安全限制：本 spike 不允许超过 15°，建议首次用 5°")

    scripts_dir, param_dir = sdk_paths()
    sys.path.insert(0, str(scripts_dir))
    from Panthera_lib import Panthera  # noqa: E402

    config, _ = load_config(args.config)
    limits = config["robot"]["joint_limits"]

    print("=" * 72)
    print("M0-1  非阻塞 HardwareLoop + 可抢占 EStop")
    print("=" * 72)
    print("正在连接机械臂（Panthera 初始化会连电机）...")
    robot = Panthera(str(param_dir / args.config))

    robot.send_get_motor_state_cmd()
    robot.motor_send_cmd()
    time.sleep(0.2)
    q0 = robot.get_current_pos()
    print(f"\n当前关节位置: {np.round(q0, 4)}")

    ji = args.joint - 1
    delta = np.deg2rad(args.deg)
    q_target = q0.copy()
    q_target[ji] = q0[ji] + delta

    lo, hi = limits["lower"][ji], limits["upper"][ji]
    if not (lo <= q_target[ji] <= hi):
        raise SystemExit(f"目标 {q_target[ji]:.3f} 超出关节{args.joint}限位 [{lo}, {hi}]，请换方向或减小幅度")

    confirm_motion(
        f"关节{args.joint} 小角度运动 + 中途急停演练",
        [
            f"以 {args.hz:.0f}Hz 非阻塞逐周期步进，把关节{args.joint} 从 "
            f"{q0[ji]:.4f} rad 移到 {q_target[ji]:.4f} rad（{args.deg:+.1f}°），计划耗时 {args.duration}s",
            f"运动进行到第 {args.estop_at}s 时置 estop 标志，循环应在下一周期调用 set_stop()",
            f"力矩上限（保守）: {SAFE_MAX_TORQUE}",
            "其余 5 个关节保持当前位置不变；不触碰夹爪",
        ],
        joint_limits={"lower": np.array(limits["lower"]), "upper": np.array(limits["upper"])},
    )

    estop_flag = threading.Event()
    t_flag_set = [0.0]

    def trip():
        time.sleep(args.estop_at)
        t_flag_set[0] = time.perf_counter()
        estop_flag.set()

    dt = 1.0 / args.hz
    periods: list[float] = []
    t_stop_done = None
    cycles_to_notice = None

    print("\n开始运动...\n")
    tripper = threading.Thread(target=trip, daemon=True)
    try:
        t0 = time.perf_counter()
        tripper.start()
        prev = t0
        i = 0
        while True:
            i += 1
            target_t = t0 + i * dt
            now = time.perf_counter()

            # ---- §1.1 步骤1：每周期最先查 estop 标志 ----
            if estop_flag.is_set():
                robot.set_stop()               # 自带 motor_send_cmd()，见核实结论 §V3
                t_stop_done = time.perf_counter()
                cycles_to_notice = i
                break

            # ---- §1.1 步骤4：推进运动一步（非阻塞，iswait=False）----
            elapsed = now - t0
            pos, vel = septic(q0, q_target, args.duration, elapsed)
            robot.Joint_Pos_Vel(pos, np.abs(vel) + 1e-3, SAFE_MAX_TORQUE, iswait=False)

            if elapsed >= args.duration:
                print("运动自然结束（未触发 estop）")
                break

            while time.perf_counter() < target_t:      # 高精度等待
                pass
            t_now = time.perf_counter()
            periods.append((t_now - prev) * 1000.0)
            prev = t_now
    finally:
        robot.set_stop()
        print("\n[finally] 已无条件 set_stop()")

    time.sleep(0.3)
    robot.send_get_motor_state_cmd()
    robot.motor_send_cmd()
    time.sleep(0.2)
    q_end = robot.get_current_pos()
    v_end = robot.get_current_vel()

    print("\n" + "=" * 72)
    print("结果")
    print("=" * 72)
    if t_stop_done is not None:
        latency_ms = (t_stop_done - t_flag_set[0]) * 1000.0
        print(f"  estop 标志 → set_stop() 返回：**{latency_ms:.2f} ms**（预算 <100ms）"
              f"  {'✅ 通过' if latency_ms < 100 else '❌ 超标'}")
        print(f"  触发时已跑周期数        ：{cycles_to_notice}")
    else:
        print("  ⚠ 未触发 estop（运动先结束），请调小 --estop-at 重跑")

    if periods:
        p = percentiles(periods)
        print(f"  实测控制周期            ：p50={p['p50']:.2f}ms p95={p['p95']:.2f}ms "
              f"max={p['max']:.2f}ms（目标 {dt*1000:.2f}ms）")
        print(f"  → 实际可达频率约        ：{1000.0/p['p50']:.0f} Hz  ← 用于锁定 §8-7 控制周期")
    print(f"  停止后关节速度          ：{np.round(v_end, 4)}（应接近 0）")
    print(f"  关节{args.joint} 起/止        ：{q0[ji]:.4f} → {q_end[ji]:.4f} rad")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
