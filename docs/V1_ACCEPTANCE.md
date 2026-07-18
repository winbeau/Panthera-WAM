# Panthera-WAM v1 收尾验收

本文是 v1 发布前的执行清单。自动化项目可由开发者直接运行；所有标记为
**真机** 的项目必须有操作员在机械臂旁，当次明确确认后才能执行。

## 1. 自动化门禁

### Python / gRPC / CLI

```bash
make check
```

通过标准：Ruff 无错误、全部 pytest 通过、`armd --sim --check` 接近 200Hz 且无异常退出。

### WPF

在原生 Windows 终端运行：

```bat
wpf\tools\run-tests.cmd
```

通过标准：Release 构建 0 warning、单元测试通过、FlaUI 在 System/Light/Dark
三种可选主题和强制 HighContrast 主题下通过，并生成四张完整 cockpit 截图。
HighContrast 由 Windows FlaUI 实际启动并截图；完整键盘焦点循环由隔离 UI 验收客户端自动执行，
不会连接 WSL bridge 或真机。

键盘验收：

- Tab 可以循环到获取/释放控制、主题、复位、EStop、MoveJ、MoveL、取消、夹爪和 12 个 jog 按钮。
- jog 按钮按住 Space/Enter 时点动，松键、失去焦点或按钮禁用时立即停止。
- `F12` 从窗口内任意焦点触发 EStop；`Esc` 取消当前长动作。

## 2. 真机前置检查

1. 清空机械臂工作空间，操作员站在急停可及位置。
2. 完成 USB attach，确认不少于 4 个 `/dev/ttyACM*`。
3. 启动服务，只做状态读取：

   ```bash
   systemctl --user start armd
   uv run panthera daemon status
   uv run panthera state get --json
   ```

4. 确认 `hardware_connected=true`、7/7 电机有效、EStop 未触发。
5. 获取专用验收 lease：

   ```bash
   uv run panthera control acquire --client-id v1-acceptance
   ```

## 3. 夹爪限位拒绝（真机，不应产生运动）

下面两个目标位于软件限位之外，预期在入队前直接拒绝：

```bash
uv run panthera gripper open --pos 2.01 --vel 0.0
uv run panthera gripper close --pos -0.01 --vel 0.0
```

通过标准：两条命令均以非零退出码结束；`reject_reason` 分别包含“目标、上限值”和
“目标、下限值”；夹爪没有动作。限位内正常命令由自动化闭环测试覆盖，本轮真机不额外动作夹爪。

## 4. MoveL 完成与取消（真机，会运动）

1. 所有位移统一用 cm 记录；先执行 FK，并从当前位置选择竖直方向目标，姿态保持不变。
   本次完整执行使用 +Z 0.3cm，放大量级取消测试使用 +Z 3cm；每次真机执行仍需操作员明确授权。
2. 先用 `cartesian plan-preview` 确认 `fraction=1`，检查关节轨迹没有越限。
3. 以不少于 2 秒的时长执行 MoveL：

   ```bash
   uv run panthera cartesian movel --pos X,Y,Z --rpy R,P,Y --duration 2.0
   ```

4. 第一次让动作完成；第二次在约 50% 时按 `Ctrl+C` 请求取消。

通过标准：

- 完成路径的 `fraction` 单调递增，终态为 `EXEC_STATE_DONE` 且最终值为 1.0。
- 取消路径经过减速收尾后进入 `EXEC_STATE_CANCELLED`，没有硬切或继续追踪旧目标。
- `EXEC_STATE_FAILED` 已由仿真断连注入测试覆盖，真机验收不主动制造硬件故障。
- 记录停止前末端误差；停止后的重力回落只记录，不作为轨迹精度失败。

## 5. 全体非持久化归零（真机，不运动，最后执行）

归零会改变本次上电周期的坐标参考，因此必须放在所有运动验收之后，执行后不再发送运动命令。

1. 记录归零前 7 个电机的位置，确认全部速度绝对值不超过 `0.01rad/s`。
2. 操作员观察机械臂并执行：

   ```bash
   uv run panthera calibrate zero --confirm
   uv run panthera state get --json
   ```

3. 确认机械臂没有物理运动，响应显示“仅本次上电有效”，状态位置变为零参考。
4. 释放 lease、停止服务并给控制器断电重启，以恢复原有非持久化坐标：

   ```bash
   uv run panthera control release
   systemctl --user stop armd
   ```

5. 重新上电、attach、启动 armd，只读状态并与步骤 1 的位置比较。恢复确认前禁止运动。

## 6. 发布签字

| 项目 | 结果 | 证据/日志 |
|---|---|---|
| `make check` | ✅ 通过：57 tests，仿真约 199Hz、0 overrun | [CI workflow](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml) |
| Windows Release + unit tests | ✅ 通过：Release 0 warning，7 项 .NET 测试 | [CI workflow](https://github.com/winbeau/Panthera-WAM/actions/workflows/ci.yml) |
| FlaUI 三态 + 强制高对比 | 通过：4 次实际启动，4 张 1240×800 截图 | `wpf-ui-artifacts` |
| Windows 高对比与完整 Tab 顺序 | ✅ 通过 | HighContrast 实际启动截图；22 个 v1 控件可 Tab 到达并循环 |
| 夹爪限位拒绝 | ✅ 通过 | `2.01 > 2`、`-0.01 < 0` 均退出码 2；操作员确认未动 |
| MoveL DONE/CANCELLED | ✅ 通过 | DONE `63298165bc7e4d699656a6335d5263a9`；CANCELLED `70d06abe097e4866b7d6c0ca45427967` |
| 全体非持久化归零及断电恢复 | ✅ 通过 | 归零无运动；断电后 7/7 有效、`fault=0`、坐标恢复 |

M2/M3/M4、M-W3 与 v1 完成口径均已收口，可创建正式版本 tag。

### 2026-07-18 真机证据

- 夹爪：以零速度发送上越限 `2.01` 和下越限 `-0.01`，服务分别返回
  “目标 2.01 超过上限 2”和“目标 -0.01 超过下限 0”；操作员现场确认夹爪未动。
- MoveL 完成：保持姿态执行 +Z 0.3cm / 2s，路径预览 `fraction=1.0`，终态
  `EXEC_STATE_DONE`。停止前末端 Z 读数变化约 0.034cm；操作员明确接受毫米级误差。
- MoveL 取消：保持姿态执行 +Z 3cm / 4s，预览 163 点、`fraction=1.0`；
  在 `fraction=0.506234` 请求取消，40 个流式样本保持单调，下一终态为
  `EXEC_STATE_CANCELLED`。取消瞬间 Z 位移约 1.245cm，随后柔顺模式下重力回落约
  1.143cm，按 M0 既定口径仅记录、不判失败。
- 全体归零：操作员确认机械臂未动；归零后 7 轴读数进入近零参考，lease 已释放。
  服务停止并断电重启后，坐标恢复为
  `[0.037071, -0.010053, -0.006283, -0.002513, 0.049009, -0.047124, -0.008168]rad`；
  7/7 状态有效、全部 `fault=0`、模式 21、控制权未持有，证明归零未持久化。
