# P2：工作零位存储、lock snapshot、proto 与 CLI

## 0. 目标

实现应用层 work-zero 的可靠读写和 `setzero`，但本阶段不实现真正回位运动。`setzero` 只保存状态，不改变硬件零点，不主动移动机械臂。

## 1. WorkZeroStore

### 1.1 文件和模型

新增建议文件：

```text
armd/src/armd/workzero.py
```

职责分离：

- `WorkZeroPose`：不可变、严格维度的数据模型；
- `WorkZeroStore`：路径解析、加载、校验、原子保存；
- `WorkZeroValidationError`：结构化拒绝原因；
- 不在 store 内读取 Backend，不在 store 内触碰 SDK。

字段要求：

```text
schema_version == 1
joints 长度 == 6
joints/gripper 全部 finite
stream_instance_id 可为空但不能是非字符串
source == teach-clutch-lock（第一版）
```

### 1.2 原子保存

保存流程：

1. 创建父目录，权限按用户目录默认值；
2. 在同一目录创建隐藏临时文件；
3. 写 UTF-8 JSON，末尾换行；
4. `flush()` + `os.fsync()`；
5. `os.replace()` 原子替换；
6. 对目录做必要 fsync（平台允许时）；
7. `chmod(0600)`；
8. 清理残留临时文件。

不得使用直接 `Path.write_text()` 覆盖正式文件作为唯一实现。

### 1.3 加载校验

拒绝：

- 文件不存在（`exists=false` 是只读查询的正常结果）；
- JSON 损坏；
- 未知 schema；
- 缺字段、额外关键字段是否允许要明确；
- NaN/Infinity；
- 维度错误；
- 位置超出当前软限位；
- 权限不可读或文件类型不是普通文件。

`stream_instance_id` 变化不自动使工作零位失效；它只是来源元数据，不是 stale motion 授权。

## 2. TeachMotion lock snapshot

### 2.1 数据结构

在 `motion.py` 或独立模块定义只读 snapshot，例如：

```text
TeachLockSnapshot
  generation: int
  joints: tuple[float, ...]
  gripper: float
  captured_monotonic_ns: int
  state: HOLD
```

要求：

- generation 初始为 0；
- 每次显式 `LOCK` 被控制循环消费时递增；
- 6 个关节和夹爪来自同一 `read_all()` 返回值；
- `drag` 不清除已经持久化的 work-zero；
- snapshot 只读复制，不能把可变 ndarray 暴露给 gRPC 线程。

### 2.2 与现有 Auto-Hold 对齐

不得复制第二套 lock 状态机。复用现有：

```text
DRAG → HOLD
DRAG → STILL_DETECT（仅自动模式）
HOLD → RELEASE → DRAG
cancel → SAFE_HOLD
```

`setzero` 只允许显式 manual-clutch teach；不能从自动 Auto-Hold 的猜测状态保存工作零位。

### 2.3 等待语义

`SetWorkZero` 服务端流程：

1. `refresh_teach_motion`；
2. 检查 active teach、manual clutch、无其它 active execution；
3. 检查 lease metadata；
4. 读取当前 generation；
5. `request_clutch(LOCK)`；
6. 在有限 deadline 内等待 generation 增加；
7. 取 snapshot，校验 7 轴和软限位；
8. 在控制线程外调用 WorkZeroStore 原子保存；
9. 返回 pose、generation、保存状态。

等待期间必须维持 lease；不能在 gRPC 线程直接读取 Backend。

## 3. proto 修改

### 3.1 先审计字段编号

在编辑 `proto/arm.proto` 前：

- 找出所有已用 field number；
- 找出 reserved number/name；
- 找出 service RPC 排列；
- 确认 C# WPF 通过 `Grpc.Tools` 从源码生成，不手工复制一份不同契约。

### 3.2 建议消息

最终名字可以按现有命名风格微调，但语义必须保持：

```proto
message WorkZeroPose { ... }
message GetWorkZeroResponse { bool exists = 1; WorkZeroPose pose = 2; string reject_reason = 3; }
message SetWorkZeroRequest { bool confirm = 1; }
message SetWorkZeroResponse { bool accepted = 1; bool saved = 2; WorkZeroPose pose = 3; uint64 lock_generation = 4; string reject_reason = 5; }
message GoWorkZeroRequest { bool confirm = 1; bool wait = 2; optional double timeout_s = 3; string reason = 4; }
```

若 `GoWorkZero` 统一返回 `ExecutionAccepted`，由 CLI 按既有 `_watch_execution` 观察；不要在 proto 里同时添加另一套同步状态流。

RPC 分类：

- `GetWorkZero`：只读，无 lease；
- `SetWorkZero`：写配置，需 lease；
- `GoWorkZero`：运动，需 lease；
- EStop 仍不需 lease；
- `rezero` 不增加重复 RPC，使用 `reason=post_action`。

### 3.3 重新生成

修改后必须执行仓库规定的：

```bash
./proto/gen.sh
```

检查：

- `proto/gen/python/panthera_arm/arm_pb2.py`；
- `arm_pb2_grpc.py`；
- `.pyi`；
- WPF 项目由相同 `arm.proto` 的 `Grpc.Tools` build 重新生成；
- 若当前 checkout 没有 WPF 工程，必须在提交说明中记录“C# 生成由 build 触发，未复制生成物”，并运行 proto 编译 smoke。

禁止手工修改 generated Python 文件。

## 4. gRPC service 接入

在 `grpc_service.py`：

- 构造 `WorkZeroStore`，路径由环境变量配置；
- 在读 RPC 中加载并返回结构化错误；
- 在写 RPC 中走统一 lease metadata；
- `SetWorkZero` 不调用既有 `SetZero`；
- `GoWorkZero` 此阶段先可以返回明确 `UNIMPLEMENTED/feature gate`，直到 P3 完成，不能伪装成已运动；
- service 启动不自动加载后运动，只加载用于查询/后续校验。

如果新增 RPC 需要更新写 RPC allow-list/interceptor，必须同步更新：

- 安全拦截器名单；
- Acquire/force-acquire 取消行为；
- CLI metadata；
- gRPC tests。

## 5. CLI

在 `cli/src/panthera_cli/__main__.py` 新增 `workzero_app`：

```text
workzero show
workzero setzero
workzero gozero
workzero rezero
```

要求：

- 复用 `load_lease/create_stub/lease_metadata/maintain_heartbeat`；
- 所有写/动命令输出明确状态和 reject_reason；
- `setzero` 不自行读 state 拼数据；
- `gozero/rezero` 默认不 silent wait，`--wait` 才观察 execution；
- `--confirm` 必须传递到服务端，不能只在客户端打印；
- CLI 不能直接访问 `PANTHERA_WORK_ZERO_PATH` 文件作为控制依据。

## 6. 测试

### Store

- round-trip；
- atomic replace；
- `0600`；
- malformed/wrong schema/non-finite/dimension errors；
- soft-limit rejection；
- missing file read-only response；
- concurrent save does not leave partial JSON。

### Teach

- lock generation only advances when lock is consumed；
- snapshot has exactly 7 values；
- gripper comes from same backend sample；
- drag does not mutate saved file；
- cancel enters SAFE_HOLD as before。

### gRPC/CLI

- no lease rejects `SetWorkZero/GoWorkZero`；
- no teach/manual clutch rejects `SetWorkZero`；
- lock timeout rejects and does not write file；
- `calibrate zero` path remains unchanged；
- generated stubs import；
- CLI help lists all four commands；
- `--confirm` absence is rejected server-side。

## 7. P2 Gate

- [ ] WorkZeroStore 单测通过；
- [ ] lock snapshot 单测证明 7 轴同样本；
- [ ] proto 重新生成、Python import、C# build/generation 路径核验；
- [ ] SetWorkZero 仿真 gRPC 流程通过；
- [ ] 未调用 `set_reset_zero`；
- [ ] 未产生真实运动；
- [ ] 提交 `feat: 增加工作零位持久化与 setzero 契约`。
