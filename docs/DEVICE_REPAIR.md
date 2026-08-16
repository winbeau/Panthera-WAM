# Pi 5 设备修复脚本

`deploy/repair-devices.sh` 用于设备拔插、服务卡死或稳定节点暂时不可用后的可重复恢复。它以 **停止并重新启动对应 systemd user service** 为主要手段，再刷新 udev、重新枚举相关节点，最后执行只读验收。

**先测后修**：`can` 目标在执行任何停止/重启前先做只读健康探测（armd active + 7 电机 mode=0x15 + fault=0 + 静止）。原本正常时直接跑只读验收并报告「✅ 机械臂服务正常」，不重启服务、不需要 sudo（避免高位无谓重启的坠臂风险）；只有电机异常（如 0x0B）才走停止 armd → udev 刷新 → 重启 armd → 验收报告正常的完整修复流程。

## 快速用法

先查看计划：

```bash
cd ~/Panthera-WAM
./deploy/repair-devices.sh c920e --dry-run
```

确认现场没有示教、录制、控制 lease 或机械臂运动后执行：

```bash
./deploy/repair-devices.sh c920e --yes
./deploy/repair-devices.sh realsense --yes
./deploy/repair-devices.sh can --yes
./deploy/repair-devices.sh all --yes
```

目标映射：

| 目标 | 设备 | 服务 | 刷新内容 |
|---|---|---|---|
| `c920e` | Logitech C920e | `overhead-camera.service` | `video4linux`、C920e 稳定别名 |
| `realsense` | Intel RealSense D405 | `camerad.service` | D405 USB、`video4linux`、D405 稳定节点 |
| `can` | Panthera CAN/串口复合设备 | `armd.service` | `ttyACM*`、权限和映射快照 |
| `all` | 上述全部 | 三个服务 | 按完整依赖顺序刷新 |

脚本会创建：

```text
~/.local/state/panthera/device-repair-*.log
~/.local/state/panthera/ttyacm-map.tsv
```

也可以通过 `--log` 和 `--map-file` 指定路径。日志和映射快照使用用户私有权限。

## 执行顺序

单设备修复只重启拥有该设备的服务，不因为 C920e 故障重启机械臂或 D405：

```text
c920e:     stop overhead-camera -> udev refresh -> start overhead-camera
realsense: stop camerad        -> udev refresh -> start camerad
can:       stop armd           -> udev refresh -> start armd
```

`all` 使用以下顺序：

```text
停止：armd -> camerad -> overhead-camera
刷新：udev rules -> ttyACM -> video4linux -> D405 USB -> settle
启动：camerad -> overhead-camera -> armd
验收：D405 -> C920e -> CAN/armd
```

停止后会等待服务实际退出、对应 gRPC 端口释放和 `udevadm settle` 完成；超时不会继续进入下一阶段。

## ttyACM 映射说明

`/dev/ttyACM0`、`/dev/ttyACM1` 等编号会随 USB 拔插变化，不能安全地写入长期控制配置。脚本会：

1. 重新加载仓库已有 udev 规则；
2. 只触发 `ttyACM*` 节点；
3. 等待 udev 完成；
4. 读取每个节点的 vendor、model、serial、`ID_PATH` 和 `DEVLINKS`；
5. 原子更新 `ttyacm-map.tsv` 作为诊断快照。

这个快照**不被 armd 用来选择电机端口**，脚本也不会自行创建可能把错误串口绑定到错误电机的永久别名。长期规则仍由：

```text
deploy/99-panthera-ht.rules
```

负责；脚本不会修改规则内容，也不会使用 `chmod 777` 替代 udev 权限。

## 安全门

真实执行必须带 `--yes`。**`--yes` 为强制模式**：跳过全部安全门（活动 lease、teach/heartbeat、collectord、运动客户端、臂速度检查），无论什么状态直接执行停止→udev 刷新→重启；操作者必须自行确认人在场、扶臂、E-stop 可触达（高位重启会坠臂）。不带 `--yes` 时默认拒绝以下情况：

- 活动 control lease；
- `teach-cal` 或 lease heartbeat 正在运行；
- `collectord` 正在录制；
- 已知的 `move`、`movej`、jog、WorkZero 或 policy 客户端正在运行；
- armd active 但无法读取 control/state；
- 机械臂当前速度超过 `0.05 rad/s`。

脚本绝不会调用：

- `control acquire` 或 `control release`；
- `move`、`movej`、jog、`ApplyPolicyChunk`；
- `setzero`、`gozero`、`rezero`；
- SDK/CAN 写接口；
- `calibrate zero` 或 `set_reset_zero`。

`--dry-run` 是强只读模式：不会调用 `systemctl stop/start`、`udevadm control`、`udevadm trigger`，也不会改变配置、规则或设备别名。

## 只读验收

- C920e：稳定别名必须是可打开的字符设备；`overhead-camera` 必须报告 `available=true`、`streaming=true`，并读取一帧 JPEG。
- D405：USB `8086:0b5b` 可见；`camerad` 必须报告腕部角色、SDK 序列号 `260422273428`、可用且正在流式传输，并读取 depth/color 各一帧。
- CAN：默认至少发现 4 个 `ttyACM*`；USB `caf1:ffff` 可见；armd 必须报告 `sim=false`、`hardware_connected=true`；7 个电机必须 `valid=yes`、`fault=0`、`mode=0x15`，且没有 lease。

RealSense 的 RSUSB 生产路径按 SDK 序列号选择设备；其 V4L2 稳定节点在某些 RSUSB 状态下可能暂时没有对应 `/dev/video*` 目标。脚本会记录这些别名状态，但最终以 `camerad` 的 SDK 角色、序列号和实际 depth/color 帧为准，不会把临时 `/dev/videoN` 写回配置。

## 退出码

```text
0  修复并通过只读验收
2  参数错误或缺少 --yes
3  环境、权限或依赖不满足
4  安全门拒绝
5  服务或 udev 操作失败
6  设备仍不可用或验收失败
7  等待服务/设备超时
8  dry-run 完成
```

如果返回 `6`，先查看日志中的最终只读状态；例如电机仍为 `mode=0x0B` 时，脚本会停止并报告异常，不会尝试通过运动命令“解锁”。
