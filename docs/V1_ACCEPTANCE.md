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
三种主题下通过，并生成三张 cockpit 截图。另在 Windows 高对比模式下手动启动一次，
确认文字、速度值、告警和焦点框均清晰可见。

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
uv run panthera gripper open --pos 2.1 --vel 0.1
uv run panthera gripper close --pos -0.1 --vel 0.1
```

通过标准：两条命令均以非零退出码结束；`reject_reason` 分别包含“目标、上限值”和
“目标、下限值”；夹爪没有动作。随后在现场允许时，用限位内的小幅目标确认正常命令仍可接受。

## 4. MoveL 完成与取消（真机，会运动）

1. 先执行 FK，并从当前位置选择竖直方向不超过 4mm 的目标；姿态保持不变。
2. 先用 `cartesian plan-preview` 确认 `fraction=1`，检查关节轨迹没有越限。
3. 以不少于 1 秒的时长执行 MoveL：

   ```bash
   uv run panthera cartesian movel --pos X,Y,Z --rpy R,P,Y --duration 1.0
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
| `make check` | 待填写 | |
| Windows Release + unit tests | 待填写 | |
| FlaUI 三主题 | 待填写 | |
| Windows 高对比与键盘 | 待填写 | |
| 夹爪限位拒绝 | 待操作员确认 | |
| MoveL DONE/CANCELLED | 待操作员确认 | |
| 全体非持久化归零及断电恢复 | 待操作员确认 | |

全部通过后，更新 `docs/MILESTONES.md` 的 M2、M3、M4、M-W3 与 v1 完成口径，
再创建正式版本 tag。
