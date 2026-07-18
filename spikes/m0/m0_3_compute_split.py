#!/usr/bin/env python3
"""M0-3：计算 / 电机 I-O 分流验证（纯计算，**不碰硬件**）。

FINAL_PLAN §1.4 假设「纯计算走独立计算 worker + 专用第二个 pinocchio 实例，
不阻塞控制循环」。本 spike 回答三个问题：

  Q1 两份 pinocchio 模型能否安全独立共存？
     （`get_Gravity` 会临时改写 `model.gravity` 再还原——共享模型下即数据竞争）
  Q2 `multi_init=True` 的 IK 墙钟耗时分布如何？→ 用于定 §1.4 的超时预算
  Q3 IK 与控制循环并发时，循环周期抖动多大？
     **线程 worker 受 GIL 影响，进程 worker 不受**——这一条可能改写架构选型。

用法（wsl-host）：
    source ~/panthera-wam-env/bin/activate
    python spikes/m0/m0_3_compute_split.py
"""
from __future__ import annotations

import contextlib
import io
import multiprocessing as mp
import threading
import time

import numpy as np

from m0_common import build_model, make_kin_shim, percentiles

CONTROL_HZ = 500.0          # §1.1 目标上限，取最严苛档位压测
PHASE_SECONDS = 3.0
IK_SAMPLES = 12


@contextlib.contextmanager
def quiet():
    """SDK 的 IK 会往 stdout 打大量收敛信息，压掉以免淹没结果。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def precise_sleep_until(t_end: float) -> None:
    """与 SDK 示例 `3_interpolation_control_zeroVel.py` 同款高精度等待。"""
    remain = t_end - time.perf_counter()
    if remain > 0.001:
        time.sleep(remain - 0.001)
    while time.perf_counter() < t_end:
        pass


def run_control_loop(shim, hz: float, duration_s: float) -> dict:
    """模拟 HardwareLoop 的每周期纯计算负载（刷新状态 + 重力前馈）。"""
    dt = 1.0 / hz
    q = np.zeros(shim.motor_count)
    periods, lateness = [], []
    t0 = time.perf_counter()
    prev = t0
    i = 0
    while True:
        i += 1
        target = t0 + i * dt
        shim.forward_kinematics(q)      # 对应 §1.1 步骤3 刷新状态缓存
        shim.get_Gravity(q)             # 对应 §1.1 步骤4 推进运动（重力前馈）
        precise_sleep_until(target)
        now = time.perf_counter()
        periods.append((now - prev) * 1000.0)
        lateness.append((now - target) * 1000.0)
        prev = now
        if now - t0 >= duration_s:
            break
    return {"period_ms": percentiles(periods), "late_ms": percentiles(lateness), "cycles": i}


def _ik_target(shim) -> np.ndarray:
    """用 FK 取一个一定可达的目标位姿，保证 IK 真的在干活而不是秒退。"""
    q_ref = np.array([0.3, 0.8, 1.0, 0.2, -0.4, 0.1])
    with quiet():
        fk = shim.forward_kinematics(q_ref)
    return np.array(fk["position"])


def _hammer_ik(shim, target, stop: threading.Event, counter: list) -> None:
    while not stop.is_set():
        with quiet():
            shim.inverse_kinematics(target_position=target, multi_init=True, num_attempts=8)
        counter[0] += 1


def _ik_worker_proc(stop_flag, counter) -> None:
    """独立进程中的计算 worker：自己建自己的模型（天然第二实例）。"""
    rm = build_model()
    shim = make_kin_shim(rm)
    target = _ik_target(shim)
    while not stop_flag.value:
        with quiet():
            shim.inverse_kinematics(target_position=target, multi_init=True, num_attempts=8)
        counter.value += 1


def main() -> int:
    print("=" * 72)
    print("M0-3  计算/电机I-O 分流验证（纯计算，不碰硬件）")
    print("=" * 72)

    # ---------- Q1: 双实例独立性 ----------
    print("\n[Q1] 两份 pinocchio 模型的独立性")
    a, b = build_model(), build_model()
    print(f"  URDF          : {a.urdf_path.name}")
    print(f"  nq/nv         : {a.model.nq}/{a.model.nv}   关节数={a.motor_count}  EEF frame={a.end_effector_frame_id}")
    print(f"  是否同一对象   : model {a.model is b.model} / data {a.data is b.data}  (期望 False/False)")

    shim_a, shim_b = make_kin_shim(a), make_kin_shim(b)
    g_before_b = b.model.gravity.linear.copy()
    orig_a = a.model.gravity.linear.copy()
    q_probe = np.array([0.1, 0.5, 0.7, 0.0, -0.2, 0.0])
    with quiet():
        shim_a.get_Gravity(q_probe)          # 内部会临时改写 A 的 gravity 再还原
    g_after_b = b.model.gravity.linear.copy()
    a_restored = np.allclose(a.model.gravity.linear, orig_a)
    b_untouched = np.allclose(g_before_b, g_after_b)
    print(f"  A.get_Gravity 后：A.gravity 已还原={a_restored}  B.gravity 未受影响={b_untouched}")

    # 交叉校验：两实例算同一个 q 应完全一致
    with quiet():
        fa = shim_a.forward_kinematics(q_probe)
        fb = shim_b.forward_kinematics(q_probe)
    consistent = np.allclose(fa["position"], fb["position"])
    print(f"  两实例 FK 结果一致 : {consistent}")
    q1_ok = a_restored and b_untouched and consistent and (a.model is not b.model)
    print(f"  → Q1 结论：双实例{'安全独立 ✅' if q1_ok else '存在问题 ❌'}")

    # ---------- Q2: IK 墙钟耗时 ----------
    print(f"\n[Q2] inverse_kinematics 墙钟耗时（multi_init=True, num_attempts=8, n={IK_SAMPLES}）")
    reachable = _ik_target(shim_a)
    t_reach = []
    for _ in range(IK_SAMPLES):
        t = time.perf_counter()
        with quiet():
            shim_a.inverse_kinematics(target_position=reachable, multi_init=True, num_attempts=8)
        t_reach.append((time.perf_counter() - t) * 1000.0)
    pr = percentiles(t_reach)
    print(f"  可达目标  : p50={pr['p50']:.1f}ms  p95={pr['p95']:.1f}ms  max={pr['max']:.1f}ms")

    unreachable = np.array([1.5, 1.5, 1.5])   # 远超工作空间 → 跑满 attempts×max_iter
    t_unreach = []
    for _ in range(max(3, IK_SAMPLES // 4)):
        t = time.perf_counter()
        with quiet():
            shim_a.inverse_kinematics(target_position=unreachable, multi_init=True, num_attempts=8)
        t_unreach.append((time.perf_counter() - t) * 1000.0)
    pu = percentiles(t_unreach)
    print(f"  不可达目标: p50={pu['p50']:.1f}ms  max={pu['max']:.1f}ms  ← 决定超时预算下限")

    # ---------- Q3: 并发下的控制循环抖动 ----------
    print(f"\n[Q3] 控制循环抖动（目标 {CONTROL_HZ:.0f}Hz = {1000/CONTROL_HZ:.1f}ms/周期，每档 {PHASE_SECONDS:.0f}s）")

    base = run_control_loop(shim_a, CONTROL_HZ, PHASE_SECONDS)
    print(f"  [基线] 无并发            : 周期 p50={base['period_ms']['p50']:.2f}ms "
          f"p95={base['period_ms']['p95']:.2f}ms max={base['period_ms']['max']:.2f}ms "
          f"({base['cycles']} 周期)")

    stop = threading.Event()
    cnt = [0]
    th = threading.Thread(target=_hammer_ik, args=(shim_b, reachable, stop, cnt), daemon=True)
    th.start()
    thr = run_control_loop(shim_a, CONTROL_HZ, PHASE_SECONDS)
    stop.set()
    th.join(timeout=15)
    print(f"  [线程] IK 在同进程线程   : 周期 p50={thr['period_ms']['p50']:.2f}ms "
          f"p95={thr['period_ms']['p95']:.2f}ms max={thr['period_ms']['max']:.2f}ms "
          f"(worker 完成 {cnt[0]} 次 IK)")

    flag = mp.Value("i", 0)
    pcnt = mp.Value("i", 0)
    proc = mp.Process(target=_ik_worker_proc, args=(flag, pcnt), daemon=True)
    proc.start()
    time.sleep(1.0)                      # 等子进程建好模型再开始计时
    prc = run_control_loop(shim_a, CONTROL_HZ, PHASE_SECONDS)
    flag.value = 1
    proc.join(timeout=15)
    if proc.is_alive():
        proc.terminate()
    print(f"  [进程] IK 在独立进程     : 周期 p50={prc['period_ms']['p50']:.2f}ms "
          f"p95={prc['period_ms']['p95']:.2f}ms max={prc['period_ms']['max']:.2f}ms "
          f"(worker 完成 {pcnt.value} 次 IK)")

    # ---------- 结论 ----------
    ideal = 1000.0 / CONTROL_HZ
    thread_overrun = thr["period_ms"]["p95"] / ideal
    proc_overrun = prc["period_ms"]["p95"] / ideal
    base_overrun = base["period_ms"]["p95"] / ideal

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    print(f"  Q1 双实例独立：{'通过 ✅' if q1_ok else '不通过 ❌'}")
    print(f"  Q2 IK 超时预算建议：≥ {max(pu['max'], pr['max']) * 3 / 1000:.1f}s"
          f"（不可达最坏 {pu['max']:.0f}ms 的 3 倍余量）")
    print(f"  Q3 p95 周期 / 理想周期：基线 {base_overrun:.2f}×，"
          f"线程 {thread_overrun:.2f}×，进程 {proc_overrun:.2f}×")
    if proc_overrun < thread_overrun * 0.9:
        print("  → 计算 worker 应使用**独立进程**：线程受 GIL 争用，进程明显更稳")
    elif thread_overrun < 1.5:
        print("  → 线程 worker 抖动可接受，可按 FINAL_PLAN §1.4 原方案（线程 + 独立模型实例）")
    else:
        print("  → 线程与进程均有明显抖动，需退化为「marshal 回 HardwareLoop + 超时预算」")
    print("=" * 72)
    return 0 if q1_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
