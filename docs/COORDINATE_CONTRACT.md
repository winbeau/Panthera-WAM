# Panthera-HT 坐标/符号契约（armd × SDK × URDF）

> 状态：**部分验证**。本文件记录 2026-08 离线审查（源码级证据）得出的契约事实，
> 并列出解除「真实 MIT teach/playback 安全门控」前必须完成的核对项。
> 所有结论均为源码/静态证据；**未做任何真机运动验证**（E-stop 保持按下）。

## 1. 已核实的契约（源码证据）

### 1.1 joint order：由 yaml 名称决定，SDK 不做重排

- `Follower.yaml` → `robot_param/motor_param/6dof_Panthera_params_follower.yaml` 定义
  `motor1..motor7`，其中 `motor1..motor6` 的 `name` 为 `joint1..joint6`，`motor7` 为夹爪。
- `Panthera._init_motors()`：`self.Motors = self.get_motors()`（CAN 板/端口顺序），
  `gripper_id = len(self.Motors)`，即**夹爪恒为列表最后一项（索引 6）**。
- `get_current_pos()/get_current_vel()/get_current_torque()` 取 `Motors[0..5]`。
- URDF `kinematics.joint_names = [joint1..joint6]`，`get_Gravity(q)` 按同名映射到
  pinocchio 模型。
- 结论：**反馈顺序 == yaml 电机顺序 == armd 索引 0..5 == URDF joint1..6**，
  **前提是** CAN 端口上物理接入的电机与 yaml 名称一致（armd 只校验数量=7，不校验名称）。

### 1.2 符号：SDK 全程无翻转

- `motor.cpp` 的 `pos_vel_MAXtqe/pos_vel_tqe_kp_kd` 只用 `*_float2int` 线性编码
  （有符号 int16），SDK 层**不存在任何 sign flip / reverse 逻辑**。
- 力矩符号约定（官方阻抗示例 `2_Jointimpendence_control_with_gra_fri_pd.py`）：
  `τ_imp = K·(q_des − q) + B·(v_des − v)`，**正力矩朝正 q 方向**，即
  「前馈力矩字段 = 电机指令力矩，与位置误差刚度同号」。

### 1.3 gravity / friction 补偿：方向、单位、适用模式

- 重力：`get_Gravity(q) = pin.computeGeneralizedGravity(model, data, q)`，
  重力方向 `[0,0,−9.81]`（Z 向下）。返回「维持该位形所需广义力矩」，
  单位 N·m，定义在 **URDF 关节轴坐标系**。
- 摩擦：`get_friction_compensation(vel, Fc, Fv, vel_threshold)`：
  `τ_f = Fc·sign(v) + Fv·v`，低速区（|v| < threshold）只用 `Fv·v`。单位 N·m。
- 用法（官方示例与 armd 一致）：`τ_total = τ_imp + G + τ_f`，直接填入
  `pos_vel_tqe_kp_kd` 的 tqe 字段（MIT 五参数帧）。**只适用于 MIT 模式**
  （`POS_VEL_TQE_KP_KD`）；`POS_VEL_TQE` 模式只有 max-torque 上限，无前馈。
- armd 实现：`Backend.compensation_torque()` = `gravity + friction`，
  `TeachMotion`/`TeachPlaybackMotion(mit)`/EStop 恢复帧均同号相加，无全局翻转。

### 1.4 armd 侧防护现状

- 软限位：`BackendLimits` 取自 `Follower.yaml`（与 armd `DEFAULT_LIMITS` 一致）；
  硬件 `pos_limit_enable/tor_limit_enable` 全部为 `false`（N3），**软限位是唯一防线**。
- 异常路径：`HardwareLoop._step_motion()` 捕获运动状态机异常后
  **显式 `backend.stop()`**（本提交修复），异常经 future 传播，
  由 `StreamExecution.error_message` / `reject_reason` 可观测。

## 2. 未验证项（解除门控前必须核对）

| # | 未验证项 | 风险 | 核对方法（静态/真机） |
|---|----------|------|----------------------|
| C1 | 电机反馈符号 vs URDF 关节轴方向 | 每关节可能差一个符号。`get_Gravity(q)` 按 URDF 轴算补偿，若电机正转方向与 URDF 轴相反，补偿力矩符号错 → teach 模式（kp=kd=0 纯前馈）下臂自行加速 | 真机 MIT 小 kp 保持 + 手推对照；或逐关节 0.1 rad/s 点动，比对 `state.velocity` 符号与关节实际转向 |
| C2 | 电机 zero offset vs URDF 零位 | `set_zero` 在任意姿态重新定义零点；URDF 零点=各关节 0 的标称位形。零点不一致 → `G(q)` 在错误位形上计算 | 记录当前零点设置时间/姿态；把臂摆到标称位形后比对 7 个电机读数 |
| C3 | CAN 端口 ↔ motor 名称映射 | armd 只校验 7 电机数量；若物理接线顺序与 yaml 不同，`joint_i` 名不副实 | 启动日志核对 `get_motor_name()`；真机逐个点动核对 J1..J6 转向 |
| C4 | 主从（leader/follower）关系 | 若后续用 leader 臂示教、follower 执行，两臂同名关节符号可能相反（SDK 官方 teleop 示例未做符号处理） | 官方 `5_teleop_control.cpp` 直接 `leader_pos → follower`，无翻转；需真机验证 |

## 3. 2026-08-12 异常复盘（teleop_validation_20260812_204713.jsonl）

- 现象：首帧 J1/J4 即非零速度；J6 从 +0.079 rad 漂至 −2.506 rad（越过软下限 −2.5）；
  约 3.24 s / 649 帧后 USB/CAN 通信板断开，armd 退出。
- 与契约的对应：该轨迹是 **TeachMotion（MIT、kp=kd=0 纯前馈）** 下录的。
  J6 持续单向漂移且无减速 ⇒ 重力/摩擦前馈在该关节上符号或幅值错误
  （C1/C2 任一成立即可解释），且 kp=kd=0 没有任何刚度/阻尼兜底。
- 追加缺陷（本提交修复）：此前 `motion.step()` 抛异常（例如测量位置越过软限位触发
  `LimitViolationError`）时只清空活动状态、**不调用 `backend.stop()`**。
- 结论：该轨迹**不能作为正式数据**；未发现「启动竞态」证据（HardwareLoop
  周期顺序 estop→cancel→refresh→requests→step 内，首帧 step 与 accepted 同周期）。

## 4. 解除门控的核对清单（全部完成才可 `--allow-unverified-teach`）

1. C1：逐关节符号核对通过（记录每关节 URDF 轴 vs 电机正方向）。
2. C2：零点核对通过（零点设置时间、与 URDF 标称位形的一致性）。
3. C3：电机名称顺序核对通过（启动日志 + 逐个点动）。
4. 在低风险姿态（如 J2/J3 接近中位、腕部朝上）用**小 kp/kd 非零**的
   TeachMotion 试运行，观测 10 s 静止漂移 < 0.01 rad 后再降到零刚度。
5. 复测 EStop 与 motion-step 异常路径（`StreamExecution.error_message` 可观测）。
6. 只有全部通过，validate_teleop 才算有真机证据；**代码测试通过 ≠ 真机已验证**。
