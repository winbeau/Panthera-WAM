# P0：前置核实、契约冻结与基线

## 0. 目的

在改 proto、motion 或真机配置前，把当前仓库事实、既有安全边界和 work-zero 新增契约核对清楚。P0 不发送任何真机运动命令，不启动新的硬件写路径。

## 1. 开始时记录环境

在新对话第一轮执行并保存结果：

```bash
cd /home/winbeau/Papers/ICLR2027-WAM-Reprojection/Panthera-WAM
git status --short
git branch --show-current
git log -5 --oneline
git rev-parse HEAD
python --version
uv --version
```

同时记录：

- 当前是否位于 Pi、WSL2 或开发机；
- `PANTHERA_*` 环境变量中与 teach、endpoint、work-zero、sim 相关的值；
- 是否存在未提交修改；
- `vendor/Panthera-HT_SDK` 的 gitlink 是否干净；
- 当前 proto 生成目录和 WPF/Grpc.Tools 消费方式。

若存在与本计划无关的未提交代码，不覆盖、不 reset，先列出影响范围。

## 2. 必须重新核实的事实

虽然 FINAL_PLAN 中已有部分“已结案”记录，触及对应代码前仍按当前 checkout 重新核对并把结论回写到 `docs/FINAL_PLAN.md`：

### 2.1 pinocchio 双实例/计算 worker

核对：

- work-zero 规划是否需要 FK/动力学；
- 若需要，是否复用现有独立计算 worker，而不是在 HardwareLoop 内做耗时 IK；
- 规划计算不能阻塞 200Hz 控制循环。

若第一版只做关节空间工作零位，不需要 IK，则在 FINAL_PLAN 明确“WorkZeroMotion 不依赖 IK；FK 仅作可选诊断”。

### 2.2 `set_reset_zero` 语义

从 SDK binding/现有文档确认：

- `set_reset_zero` 改变硬件参考，不是应用工作零位；
- 本功能不得调用它；
- `calibrate zero` 与 `workzero setzero` 的命令、文档和测试必须分开。

不能因为名字相似而复用既有 `SetZero` RPC。

### 2.3 继承层签名和 MotorState

核对真实字段和状态有效性规则：

- 6 个 arm motor + 1 个 gripper 的顺序；
- `valid`、`fault`、`mode`、`position`、`velocity`、`torque` 的含义；
- `position == 999.0` 哨兵不得进入工作零位文件或目标帧；
- 软限位来自 `Backend.limits`，不可从客户端输入盲信。

### 2.4 当前帧和固件约束

逐个确认以下调用链：

- `JointPositionMotion.step()` 是否仍是单次 `position_frame`；
- `JointJogMotion` 的短前瞻路径和新鲜度窗口；
- `JointMITMotion` 的完整帧校验和过期处理；
- `TeachMotion` 的 MIT 帧、lock/drag/SAFE_HOLD；
- `HardwareLoop` 的 EStop→cancel→refresh→motion 顺序；
- `ExecutionRegistry` 的注册、观察、取消。

将 WorkZeroMotion 的允许路径写成明确的代码审计规则：

```text
允许：HardwareLoop.step -> WorkZeroMotion -> Backend.write_frame(JointFrame MIT)
禁止：WorkZeroMotion -> position_frame()
禁止：WorkZeroMotion -> MoveJ/JointMove/RunJointTrajectory
禁止：CLI 直接写 Backend/SDK
```

## 3. 设计选择冻结

### 3.1 存储

冻结为 `armd/src/armd/workzero.py`：

```python
@dataclass(frozen=True, slots=True)
class WorkZeroPose:
    schema_version: int
    joints: tuple[float, ...]       # exactly 6
    gripper: float
    captured_at_ms: int
    sampled_monotonic_ns: int | None
    state_sequence: int | None
    stream_instance_id: str
    source: str                     # teach-clutch-lock
```

默认路径使用项目已有约定 `~/.config/panthera-wam/work-zero.json`，由 `PANTHERA_WORK_ZERO_PATH` 覆盖。schema 版本必须可拒绝未知版本。

### 3.2 RPC

第一版增加：

```text
GetWorkZero
SetWorkZero
GoWorkZero
```

`rezero` 是 CLI/session 语义别名，调用 `GoWorkZero`，不复制运动实现。若现有 proto 风格要求 response 统一，可让 `GoWorkZero` 返回 `ExecutionAccepted`；具体字段编号先查完整 proto 后再定。

### 3.3 lock snapshot

在 `TeachMotion` 内新增只读 snapshot/generation：

- 每次显式 lock 成功处理时 generation 加一；
- snapshot 同时保存 6 个 arm 位置和 gripper 位置；
- snapshot 由同一次 `backend.read_all()` 得到；
- `SetWorkZero` 请求 lock 后等待 generation 改变；
- 超时、teach 终止、lease 失效都拒绝保存。

不要通过两个独立 RPC 读取 arm/gripper。

### 3.4 WorkZeroMotion 初版范围

- 关节空间，不做 Cartesian/IK；
- 目标为 6 个 arm 关节 + gripper 工作姿态；
- 服务端生成连续 MIT 完整帧；
- 使用保守固定默认增益、速度/加速度/力矩限制；
- 目标、当前状态、限位和 EStop 在服务端二次校验；
- 小残差、目标过近、异常 mode 的策略先在仿真明确，不能到真机现场临时猜。

## 4. 基线检查

只运行不接触真机的检查：

```bash
cd armd
uv run pytest -q
uv run ruff check src tests
uv run python -m compileall -q src tests
cd ../cli
uv run pytest -q
uv run ruff check src tests
cd ..
bash -n deploy/preview-record.sh
git diff --check
```

仓库若有既定更窄的检查命令，以实际 pyproject/AGENTS 为准；不要因 P0 重跑与当前改动无关的长套件。

对 proto 做导入基线：

```bash
uv run --directory proto python -c "from panthera_arm import arm_pb2, arm_pb2_grpc; print('proto import ok')"
```

若工具缺失，不在 P0 私自安装系统依赖；按项目要求停下并报告安装命令。

## 5. P0 文档变更

在 `docs/FINAL_PLAN.md` 增加短节，记录：

1. work-zero 是应用层 pose，不是 `set_reset_zero`；
2. `gozero/rezero` 使用服务端连续 MIT 状态机，禁止单帧 POS-VEL；
3. action window 的起止定义；
4. armd 启动不自动运动；
5. 当前小残差/夹爪回位策略和待真机验证项；
6. proto/CLI 命令树的新位置。

在 `docs/JOINT_CONTROL.md` 增加“WorkZero 真机前置”小节，但不写未经验证的速度或力矩数值为既定事实。

## 6. P0 停止条件

出现下列任一情况，停止实现并记录原因：

- 当前 proto 已有同名 field/RPC，无法安全追加；
- 当前工作树存在无法归属的 motion/backend 修改；
- 真实 MotorState 顺序或 gripper 字段与计划不一致；
- 不能证明 WorkZeroMotion 每周期走 HardwareLoop；
- 只能通过 SDK 子模块修改才能实现；
- 需要复用单帧 `position_frame` 才能完成回位；
- 基线测试已失败且失败与当前基线/环境无关。

## 7. P0 Gate

P0 通过必须同时满足：

- [ ] 事实核实结论已写回 FINAL_PLAN；
- [ ] WorkZeroPose、RPC 形状、lock snapshot 方案已冻结；
- [ ] 禁止路径清单能对应到代码搜索结果；
- [ ] 基线测试/导入结果已记录；
- [ ] 未发送真机运动命令；
- [ ] 提交 `docs: 冻结 work-zero 实现契约与验证门`。
