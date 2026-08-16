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
- **分段补偿默认关闭**：只有显式设置 `PANTHERA_TEACH_GRAVITY_SEGMENTED=1`
  才启用 `PANTHERA_TEACH_GRAVITY_SCALE_HIGH/BREAKPOINT`。固定断点会让补偿力矩不连续，
  可能制造人工平衡/吸附点（J2 约 69°尤其危险）；未完成连续曲线标定前不要开启。
- 连续模型若在过轴后仍有近似恒定残差，可使用 `PANTHERA_TEACH_GRAVITY_RESIDUAL`
  逐关节增加 Nm 偏置；默认全零，先用多位置静态数据确认符号，不要用它替代 scale 标定。
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

## 5. 参数调节速查表（每关节 kp/kd/fc/fv 的 +/− 效果）

teach 每关节 4 个参数：**kp**（刚度，回中力）、**kd**（速度阻尼）、**fc**（库伦摩擦补偿，恒定向助力）、**fv**（粘性摩擦补偿，随速度助力）；承重关节另有 **gravity_scale**（env 配置，重力补偿缩放）。调参顺序：**先 scale（重力）→ 再 fc/fv（摩擦）→ 最后 kd/kp（收尾手感）**。现场标定工具：`deploy/teach-cal.sh`（改参→重启→拉起 teach 一条龙）。

### 转动关节（J1 肩旋 / J5 腕滚 / J6 末滚：竖直/滚转轴，重力矩≈0）

#### J1（轴 +Z，M5047 21Nm）当前基线：fc=0.05 fv=0.02 kp=kd=0（scale 无效）

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| kp | 拖离启动位置有回中力，越远越强 | 无回中，拖哪停哪（默认 0） |
| kd | 拖动带阻力、放手很快停住 | 更丝滑、放手滑行远/不停 |
| fc | 拨动后被助力续转（0.20 时乱转） | 起步稍涩、放手靠真实摩擦更快停 |
| fv | 快速拨动被推着加速 | 高速拖动显现真实粘性阻力 |

#### J5（轴 −Z，M4438 10Nm）当前基线：fc=0.02 fv=0.01 kp=kd=0（scale 无效）

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| kp | 末端滚转回中力 | 自由（默认 0） |
| kd | 滚转带阻力、放手快停 | 丝滑、滑行远 |
| fc | 拨动后续转（0.04 时也会） | 起步涩、更快停 |
| fv | 高速滚转被助力 | 高速显真实阻力 |

#### J6（轴 +X，M4438 10Nm）当前基线：fc=0.02 fv=0.01 kp=kd=0（scale 无效）

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| kp | 末端滚转回中力 | 自由（默认 0） |
| kd | 滚转带阻力、放手快停 | 丝滑、滑行远 |
| fc | 拨动后续转 | 起步涩、更快停 |
| fv | 高速滚转被助力 | 高速显真实阻力 |

### 承重关节（J2 上臂 / J3 前臂 / J4 腕俯仰：水平轴，重力矩大）

#### J2（轴 +Y，M6056 36Nm）当前基线：scale=0.85 fc=0.15 fv=0.06 kp=kd=0

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| scale | 补偿增强→臂被抬起（过补偿） | 臂下垂（欠补偿） |
| kp | 回中力稳定位形（拖离被拉回） | 自由（默认 0） |
| kd | 拖动阻力、放手快停不下坠 | 丝滑、放手慢/下滑 |
| fc | 拖动被助力；过大放手自驱 | 起步涩 |
| fv | 高速拖动被助力 | 高速显真实阻力 |

#### J3（轴 −Y，M6056 36Nm）当前基线：scale=1.0 fc=0.15 fv=0.06 kp=kd=0

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| scale | 前臂被抬起（0.85 时下垂撑不住） | 前臂下垂 |
| kp | 回中力 | 自由（默认 0） |
| kd | 拖动阻力、放手快停 | 丝滑、慢停 |
| fc | 拖动助力 | 起步涩 |
| fv | 高速助力 | 高速阻力 |

#### J4（轴 −Y，M5047 21Nm）当前基线：scale=0.7 fc=0.15 fv=0.03 kp=kd=0

| 参数 | 调大 (+) | 调小 (−) |
|---|---|---|
| scale | 手腕被抬起（1.0 时上抬漂移） | 手腕下垂 |
| kp | 回中力 | 自由（默认 0） |
| kd | 拖动阻力、放手快停 | 丝滑、慢停 |
| fc | 拖动助力 | 起步涩 |
| fv | 高速助力 | 高速阻力 |

### 调参口诀

- 滑/自驱 → 减 fc（转动轴）或减 scale（承重轴）
- 下垂/欠支撑 → 增 scale（承重轴）
- 涩/起步费力 → 增 fc（该轴真实摩擦大）
- 黏/拖不快 → 减 fv
- 飘/停不住 → 增 kd（不动 kp）
- 要回中/稳定 → 增 kp（会牺牲拖拽顺滑）
- 一次只调一组参数，每次改完拖 3-5 次感受；转动轴与承重轴分开调

## 6. 当前标定配置（2026-08-12 定稿）

真机徒手拖动手感验收通过后的正式配置，已写入 `deploy/teach-cal.sh` 出厂基线
（`--reset` 恢复）与 Pi 状态文件 `~/.config/panthera-wam/teach-cal.json`。
标定工具：`~/Panthera-WAM/deploy/teach-cal.sh --J1 "0,0.1,-0.02,0,0"`（增量，0 不改，
顺序 kp,kd,fc,fv,scale），`--show` 查看、`--reset` 恢复基线。

| 关节 | kp | kd | fc | fv | scale | 备注 |
|---|---|---|---|---|---|---|
| J1 肩旋 | 0 | 0.40 | 0.05 | 0.02 | 0.85* | 转动轴，稍带阻力松手即停 |
| J2 上臂 | 0 | 0.55 | 0.15 | 0.06 | 0.85 | 承重轴，防跳松手即停 |
| J3 前臂 | 0 | 0.60 | 0.15 | 0.06 | 1.15 | 承重轴，防跳；scale 高于其余承重轴 |
| J4 腕俯仰 | 0 | 0.40 | 0.15 | 0.03 | 1.0 | 承重轴，防跳 |
| J5 腕滚 | 0 | 0.15 | 0.02 | 0.01 | 0.85* | 转动轴 |
| J6 末滚 | 0 | 0.08 | 0.02 | 0.01 | 0.85* | 转动轴，最轻阻尼 |

*J1/J5/J6 竖直/滚转轴重力矩≈0，scale 无实际影响（占位）。

采集（collectord）时应先以本配置 `teach start`（kp=kd 用上表值），确保拖拽时
臂不会自驱/下垂；正式录制如需最丝滑手感可临时将 kd 减半（旋转轴），录完恢复。

### Auto-Hold 状态机与显式离合（lock/drag，推荐采集使用）

teach 模式内有一个 Auto-Hold 状态机，负责「手拖（柔顺）↔ 锁位（刚硬）」切换。
状态机共 5 个状态：

| 状态 | 含义 | 进入条件 | 下发控制帧 |
|---|---|---|---|
| `DRAG` | 手拖：kp=0，只发重力/摩擦前馈 | teach 启动；`drag` 释放完成 | 实时 q/v 前馈 |
| `STILL_DETECT` | 自动模式：疑似松手（速度已低但未确认） | 全关节 \|v\| < `still_velocity_threshold` | 同 DRAG |
| `HOLD` | 锁位：位置刚度保持 | 自动：静止持续 `still_duration`；显式：`lock` 命令 | 锚定 q_hold + kp_hold |
| `RELEASE` | 平滑退出 HOLD（kp 渐变回 0） | 自动：任一关节 \|v\| > `release_velocity_threshold`；显式：`drag` 命令 | kp/kd 渐变 |
| `SAFE_HOLD` | teach 被取消后的限时安全保持 | `teach stop` / lease 过期 / `pkill heartbeat` | 锚定当前位形 + kp_hold，约 10s 后 limp |

两种驱动模式：

- **自动 Auto-Hold（默认开启）**：靠速度阈值推测「松手」，适合原地短暂松手。
  重力残差驱动的慢漂移与真实手推在低速下不可区分，因此自动判定**不保证**任意位形松手即停。
- **显式离合（推荐采集使用）**：`lock`/`drag` 命令直接驱动 HOLD/RELEASE，**不依赖速度**；
  松手/继续拖由操作员意图决定，锁位可靠。teach-cal.sh 启动时自动带 `--manual-clutch`。

```bash
./deploy/teach-cal.sh                  # 启动显式离合 teach（重启 armd + 拉起）
./deploy/teach-cal.sh lock              # 下一控制周期采样当前位置并锁定（进入 HOLD）
./deploy/teach-cal.sh drag              # 平滑释放位置刚度，恢复手拖（经 RELEASE 回 DRAG）
./deploy/teach-cal.sh stop              # 优雅停止（先 SAFE_HOLD 约 10s，请扶住机械臂）
pkill -f "control heartbeat"            # 等价停止：lease 过期同样进入 SAFE_HOLD
```

**显式离合保持参数**（代码常量，改动需提交 `armd/src/armd/motion.py`）：

| 参数 | 值 | 说明 |
|---|---|---|
| `MANUAL_CLUTCH_KP_HOLD` | `[4, 20, 20, 2, 2, 1]` | 锁位刚度（J2/J3 承重轴 20，压住重力残差稳态偏移≈残差/kp） |
| hold/release ramp | 80 ms smoothstep | kp 渐变，禁止瞬间跳变 |
| HOLD 阻尼 | `max(拖动 kd, kd_hold, kp*0.08)` | 不低于拖动阻尼，防止欠阻尼振荡 |
| 前馈锚定 | q_hold + 零速度 | 重力项不随漂移追着走，摩擦项不助推漂移 |
| `SAFE_HOLD` 时长 | 10 s（`PANTHERA_TEACH_SAFE_HOLD_S`） | 取消后保持，超时退出到零前馈柔顺阻尼 |

**自动模式阈值参数**（`AutoHoldConfig`，`armd/src/armd/motion.py`，默认值）：
`still_velocity_threshold=0.02 rad/s`、`release_velocity_threshold=0.04 rad/s`、
`still_duration=0.20 s`、`hold_ramp_time=0.40 s`、`release_ramp_time=0.20 s`、
`kp_hold=[1,2,2,1,0.8,0.8]`、`kd_hold=[0.4,0.8,0.8,0.4,0.2,0.2]`。

**与 §5 调参的关系**：`DRAG` 阶段的手感完全由 §5 的 kp/kd/fc/fv/scale 决定
（teach start 下发 kp/kd，fc/fv/scale 来自 `armd.env`）；`HOLD` 阶段刚度与拖动参数独立，
调锁位硬度改 `MANUAL_CLUTCH_KP_HOLD`。采集时在每次放置/调整末端后执行 `lock`，
拖到下一位置前执行 `drag`。

**⚠ 安全注意**：`SAFE_HOLD` 的 10s 保持已在代码实现并有仿真回归，但**真机保持效果尚未现场确认**；
停止 teach 时务必扶住机械臂，高位停止尤其不要站在承重关节下方。
`lock` 现场验证（kp=20）：J2 高位约 6 秒零速度保持；若仍观察到缓慢漂移，属于重力前馈残差，
应调整 `PANTHERA_TEACH_GRAVITY_RESIDUAL`（符号/数值）而非盲目增大 kp。

## 7. WorkZero 真机前置（work-zero 方案，2026-08-17 冻结）

进入工作零位 / 回位（`workzero gozero/rezero`）的安全前置：

1. **只读前置**：`armd.service` active、7 电机 valid/fault=0/mode 正常、EStop 状态符合预期、
   `~/.config/panthera-wam/work-zero.json` 存在且权限 0600、C# 前端与其它 D405 客户端关闭。
2. **gozero/rezero 回位路径**（2026-08-16 真机验收修订）：MoveL 轨迹到工作零位 +
   到位后自动切 teach manual-clutch LOCK 定住（开爪在 HOLD 帧内由脚本完成）。
   真机仍禁止作为回位路径：`move/movej`、`JointPositionMotion`、单帧 `position_frame`、
   teach play 的 posvel 起点移动、`trajectory run-waypoints`。
   回位流程：gozero = 初始位→MoveL工作0位→teach lock→开爪；
   rezero = 动作完成位(teach HOLD)→开爪（松方块）→快速退出 teach→MoveL工作0位→teach lock。
3. **显式确认**：每次真机 gozero/rezero 前脚本先打印 target、duration、limits，
   用户二次确认（`--confirm` 由服务端判定，不能只是客户端提示），E-stop 可立即触达。
4. **低速/零速 MIT hold 未完成真机验证前**，不把 gozero 目标设为极小位移；
   `PANTHERA_WORKZERO_REAL_HARDWARE_ENABLED` 默认 0，真机放行需逐级确认。
5. **setzero** 基于 active teach + 显式 manual clutch lock 的同一 7 轴状态样本，
   不调用 `calibrate zero` / `set_reset_zero`。
6. **失败语义**：EStop/lease 失效后不自动恢复、不自动 rezero；报告 rezero=skipped 与原因。

## 4. 安全须知

- **低速 jog 会锁死关节**：任何低于 0.1 rad/s 的单关节命令都视为危险操作；锁死后必须
  重启 armd（电机在 150ms 固件看门狗内进入阻尼，重启本身不产生运动）。
- 硬件限位保护全部关闭（`pos_limit_enable=false`），**软限位是唯一防线**；目标位置越限
  RPC 直接拒绝（结构化 reject_reason）。
- 物理 E-stop 优先于一切软件路径；操作员在场是运动测试的前提。
- 真实 MIT teach/playback 当前默认拒绝（坐标契约门控）；不要用 `--force` 或绕过门控。
