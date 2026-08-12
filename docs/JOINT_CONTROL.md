# Panthera-HT 关节控制手册

> 真机实测经验（2026-08-12 现场，selabpi5 / armd c5ac106）。记录本路的 bug 解决、
> 正确 CLI 控制姿势与坐标符号契约核对结果。**只要动真机就先读本文**。
> 配套背景：`docs/FINAL_PLAN.md`（架构）、`docs/COORDINATE_CONTRACT.md`（契约核对清单）。

## 1. 正确 CLI 控制方法

### 1.1 前置：lease（控制权）与心跳

armd 的 lease 默认 **2 秒超时**。`acquire` 之后不维持心跳，任何运动 RPC 都会报：

```
PERMISSION_DENIED: 缺少或无效的控制权 lease
```

正确姿势（**acquire 后并行跑 heartbeat**，运动命令用同一 token）：

```bash
cd ~/Panthera-WAM
uv run --no-sync --package panthera-cli panthera control acquire --client-id operator   # 一次
uv run --no-sync --package panthera-cli panthera control heartbeat >/dev/null 2>&1 &    # 后台维持
HB=$!
# ……执行运动命令……
kill $HB
```

`joint jog`（流式 RPC）每条命令（0.05s 间隔）会自动续期 lease，`--duration` 到期自动
停止；`joint move/movej --wait` 内部也续期，但 acquire 到命令之间的空窗仍会过期，
所以 heartbeat 后台进程不能省。

### 1.2 单关节点动（jog）

```bash
panthera joint jog --vel "0,0,0,0.3,0,0" --duration 2
```

- `--vel` 必须**逗号分隔**的 6 个数（空格分隔报错：`vel 必须是逗号分隔数值`）。
- **速度 ≥ 0.3 rad/s，位移 ≥ 10°（0.175 rad）**。低于摩擦死区会触发固件堵转锁死
  （见 §2 B1），承重关节（J2/J4）尤其明显；J1（竖直轴无重力矩）小速度侥幸能走。
- jog 是 POS-VEL 短前瞻，`--duration` 结束自动减速；`limit_hit` 为空表示未触软限位。

### 1.3 位置模式（move / movej）

```bash
panthera joint movej --pos "0.0,0.0,0.0,0.0,0.0,0.0" --duration 3 --wait --tolerance 0.01
```

- `--pos` 是**绝对关节位置**（rad）；`movej` 按 `(目标-当前)/duration` 计算速度，只下发一次
  目标帧（SDK 语义），之后轮询到位。
- 到位后固件 PID 保持，不会像柔顺模式那样重力回落——**验证到位/符号请用 movej**。
- 其它关节必须传当前值（或同时规划），只动单轴时其余关节 vel 为 0 保持。

### 1.4 状态读取

```bash
panthera state get        # 7 电机位置/速度/力矩/mode/fault
panthera daemon status    # sim、控制频率、硬件连接
```

- `valid=yes` 表示编码器在线；`mode=0x15` 是正常控制模式（0x00 空闲查询、**0x0B = 异常**）。
- 关节位置 `≈0` 即 URDF 零位（零点出厂设置，armd 运行期无 set_zero 记录）。

## 2. Bug 记录与解决

### B1【最严重】低速 jog 触发固件堵转锁死（mode 0x0B）

| 项 | 内容 |
|---|---|
| 现象 | 单动 J4 用 `0.03–0.05 rad/s` jog：反馈位置几乎不动 → 偶尔「跳一下」→ 之后该关节 mode 变 `0x0B`，**所有后续指令（jog/move/movej 45°）全部不响应**，位置误差恒定 |
| 恢复 | `systemctl --user restart armd.service`（重新初始化 SDK 电机枚举）→ mode 回到 0x15；**再次低速命令会再次锁死** |
| 根因 | 速度低于摩擦死区 → 编码器几乎不动 → 固件判定堵转 → 进入异常模式锁死（疑似固件堵转保护） |
| 排除项 | 非线缆接触不良（WPF 按住 Jog J4 稳定）；非 SDK 槽位映射（`MEM_INDEX_ID(id)=id-1` 正确）；非关节硬件故障（0.3 rad/s 完全正常） |
| 正确做法 | 单关节 jog 速度 **≥ 0.3 rad/s**；测试位移 **≥ 10°（0.175 rad）**（与 `docs/FASTWAM_COLLECTION.md` 的硬件实验容差一致）；J1 竖直轴除外但也不鼓励低速 |
| 教训 | 现场出现「某关节跳一下就不动」时，先怀疑**低速死区锁死**再怀疑硬件；同一 armd 下 WPF/CLI 走相同 RPC，用 WPF 对照可快速排除指令路径差异 |

### B2 lease 2 秒过期导致 PERMISSION_DENIED

- 现象：`control acquire` 后隔几秒执行 `joint jog` 报 `PERMISSION_DENIED`。
- 根因：armd `lease_timeout_s` 默认 2.0s；CLI 不自动维持心跳。
- 解决：运动前并行 `control heartbeat`（§1.1）；或命令紧跟在 acquire 之后。

### B3 CLI `--vel` 参数必须逗号分隔

- 空格分隔（`"0.03 0 0 0 0 0"`）报错：`Invalid value: vel 必须是逗号分隔数值`。
- 正确：`"0.03,0,0,0,0,0"`。

### B4 柔顺阻尼下的重力回落（正常行为，不是 bug）

- armd 空闲时下发零刚度软件阻尼帧：**静止力矩 ≈ 0，承重关节（J2/J3/J4）受重力自然回落**，
  竖直轴（J1/J5）停得住。jog 结束后立即读状态，承重关节位置可能已回落——**不要据此误判
  「指令没生效」**；验证运动请用 `movej --wait`（PID 保持）或运动过程中观察。
- 8-12 事故（J6 漂移越软限位）同属「无刚度前馈/柔顺窗口」类问题，见 `docs/COORDINATE_CONTRACT.md` §3。

## 3. 坐标/符号契约（C1 实测，2026-08-12）

逐关节 `+0.3 rad/s × 2s` jog，反馈位置增量与用户目视方向比对 URDF 轴：

| 关节 | URDF 轴 | 正方向实测 | 结果 |
|---|---|---|---|
| J1 | +Z | 俯视逆时针 | ✅ 一致 |
| J2 | +Y | 上臂抬起 | ✅ 一致 |
| J3 | -Y | 前臂上抬 | ✅ 一致 |
| J4 | -Y | 手腕上抬 | ✅ 一致 |
| J5 | -Z | 俯视顺时针 | ✅ 一致 |
| J6 | +X | 前端看逆时针（末端滚转） | ✅ 一致 |

**结论：电机反馈符号 = URDF 关节轴符号，无符号反转。** 重力/摩擦补偿同号相加的方向
约定有 SDK 源码证据（`Panthera.py::get_Gravity/get_friction_compensation` 与官方阻抗示例）。

C2（零点）状态：7 关节回零后全部读数在 ±0.02 rad 内；armd 日志（2026-08-01 起）无
`set_zero` 记录，判定零点为出厂默认。**目视位形确认后即可关闭 C2**，届时真实 MIT
teach/playback 门控可按 `docs/COORDINATE_CONTRACT.md` §4 清单解除
（`--allow-unverified-teach` / `PANTHERA_ALLOW_UNVERIFIED_TEACH=1`）。

### 重力补偿标定（2026-08-12 实测）

teach 试运行（kp/kd 非零）实测：**J2 从 0.089 漂移到 1.466 rad（≈78°）后被 kp 拉住，
J4 漂到 0.62，J3 稳定**——J2/J4 重力补偿**过强**，J3 正确。平衡点拟合得实际重力矩
≈ **0.7 × G(q)**（URDF 惯性参数偏大约 30%）。

- 修复：`PANTHERA_TEACH_GRAVITY_SCALE`（`--teach-gravity-scale`）缩放重力项，
  摩擦项不缩放；默认 1.0。
- 标定流程：设 scale=0.7 → teach 小 kp/kd（如 0.3/0.1）→ 用户松手观测 10s →
  迭代 scale 直到静止漂移 < 0.01 rad/s。**不要凭猜测改符号或全局翻转力矩**。

### B5【严重】move/movej（单次目标帧）触发固件锁死

| 项 | 内容 |
|---|---|
| 现象 | `joint move` / `joint movej`（JointPositionMotion 只下发一次目标帧）在**任何速度**下都会锁死：目标关节不动（误差恒定），全板 mode 变 `0x0B`，需 `systemctl --user restart armd` 恢复 |
| 对照 | 同关节用 `joint jog`（流式短前瞻）完全正常（J2 0.3 rad/s jog 正常运动 0.35 rad） |
| 根因 | 当前固件（4.7.3）的 POS_VEL 单次目标执行路径不可靠（与 AGENTS.md「SDK/MIT 路径在当前固件上跟踪失败」一致），疑似固件侧保护/异常状态 |
| 正确做法 | **真机运动一律用 `joint jog`（≥0.3 rad/s），不要用 move/movej 验证或控制**；move/movej 的 JointPositionMotion 修复（改为流式下发）前视为真机不可用 |
| 影响 | teach 录制/回放不受影响（MIT 流式帧）；PolicyChunkMotion 走 position_frame 单帧——**真机策略执行前必须先修** |

## 4. 安全须知

- **低速 jog 会锁死关节**：任何低于 0.1 rad/s 的单关节命令都视为危险操作；锁死后必须
  重启 armd（电机在 150ms 固件看门狗内进入阻尼，重启本身不产生运动）。
- 硬件限位保护全部关闭（`pos_limit_enable=false`），**软限位是唯一防线**；目标位置越限
  RPC 直接拒绝（结构化 reject_reason）。
- 物理 E-stop 优先于一切软件路径；操作员在场是运动测试的前提。
- 真实 MIT teach/playback 当前默认拒绝（坐标契约门控）；不要用 `--force` 或绕过门控。
