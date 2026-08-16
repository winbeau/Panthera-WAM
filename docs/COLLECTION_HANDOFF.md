# Panthera-WAM LeRobot 数据采集交接文档

> 接手人：请从 §1 环境检查开始，按 §3/§4 流程操作。所有真机运动必须人在场、
> E-stop 可触达；收工或重启 armd 前必须先 `zero-home`（见 §8 事故纪律）。

## 1. 交接 commit 与灾难回滚

| 内容 | 值 |
|---|---|
| 交接 commit（HEAD） | `b13c39f` feat: 数据采集交接一行命令 + 变长录制 + 夹爪调紧工具 |
| 上一稳定点（夹爪锁存修复） | `ae0c45e` fix: 夹爪到位后持久锁存停止继续开爪 |
| 回位链稳定点 | `5385c30` fix: MoveL plan 参数错误恢复 INVALID_ARGUMENT 契约（含先算后放、连续插值、先于 90% 钳位） |
| Pi 本地仓库 | `~/Panthera-WAM`（origin `github.com/winbeau/Panthera-WAM.git`，branch `main`） |
| Pi 当前部署 | `b13c39f` 已 pull；armd 运行代码为 `ae0c45e`+（无需为新脚本重启） |

回滚步骤（Pi 上，**先让臂回低位**）：

```bash
cd ~/Panthera-WAM
./deploy/lerobot-collect.sh zero-home   # 必须先回低位（高位形重启=坠臂）
source /home/winbeau/sb/env.sh && proxyon >/dev/null 2>&1
git reset --hard ae0c45e                # 或 5385c30 / 任意已验证 commit
uv sync --frozen
systemctl --user restart armd.service
```

恢复前沿：`git pull --ff-only origin main`；改 armd 代码后必须重启 armd 才生效
（`git pull` 不会热加载）。重启前确认 `panthera state get` 里 J2≈0/J3≈0（低位）。

Pi 的 `~/.config/panthera-wam/armd.env` 关键项：

```text
ALLOW_UNVERIFIED_TEACH=1
WORKZERO_ENABLED=1
WORKZERO_REAL_HARDWARE_ENABLED=1
PANTHERA_VELOCITY_LIMIT_SCALE=8
PANTHERA_ACCELERATION_LIMIT_SCALE=8
```

## 2. 环境与前置检查

```bash
cd ~/Panthera-WAM
export PANTHERA_ENDPOINT=127.0.0.1:50051
uv sync --frozen                                   # 修复过 venv 被 uv 重建导致 CLI 丢失
systemctl --user is-active armd.service            # 必须 active
./.venv/bin/panthera state get                     # 7 电机 valid/fault=0/mode=0x15
./.venv/bin/panthera workzero show                 # 工作零位已保存（gripper 保存值 1.9735）
```

工作零位只保存一次（`workzero setzero`，需 teach manual-clutch + lock），
交接后一般不需要重存；开爪目标运行时自动钳位到 90%（1.8）。

## 3. Preview 录制流程（六条一行命令）

所有命令在 `~/Panthera-WAM` 下执行。命令 = `./deploy/lerobot-collect.sh <子命令>`。

| 步骤 | 终端 | 一行命令 | 说明 |
|---|---|---|---|
| 1 | 任一 | `gozero` | MoveL 回工作0位 → 定死锁 + 开爪 90% |
| 2 | A | `start-record color-block 021` | 定死锁→阻尼锁（teach start+lock），后台开始 preview 录制 |
| 3 | B | `drag` / `lock --gripper 0.2` | 手拖动作；抓取/放置位闭爪 10% + 阻尼锁 |
| 4 | A | `end-record` | SIGTERM 优雅结束（**变长**，窗口=实际动作）→ 阻尼锁 + 开爪 90% |
| 5 | 任一 | `rezero` | 开爪松方块 → MoveL 回工作0位 → 定死锁 |

终端 B 的动作循环（在步骤 2 之后、步骤 4 之前）：

```bash
./deploy/lerobot-collect.sh drag                     # 手拖到方块
./deploy/lerobot-collect.sh lock --gripper 0.2       # 闭爪抓取（脚本闭爪，动作不包含闭爪）
./deploy/lerobot-collect.sh drag                     # 拖到目标区上方
./deploy/lerobot-collect.sh lock --gripper 0.2       # 放置位闭爪保持
```

产物：`~/panthera-data/preview/color-block_021/`
（`trajectory_021.jsonl` 原始 7 轴 + `replay_trajectory_021.jsonl` TeachPlay 视图 +
两个 MP4 + `preview.json`）。`end-record` 会用 `preview.json` 的 `success` 字段验收。
其中 `duration_s` 是安全上限，`actual_duration_s` 才是本次动作窗口；状态流和相机
帧数质量门按 `actual_duration_s` 计算。两个 MP4 的 PTS 来自 Pi 单调相机时间戳，
即使编码掉帧也不得压缩或加速动作时间线。

## 4. 正式录制（record-formal）

正式录制 = 变长 collectord episode + **机械臂自动回放 preview 动作**，
开始与结束都处于阻尼锁：

```bash
./deploy/lerobot-collect.sh record-formal color-block-000011 \
    ~/panthera-data/preview/color-block_021/replay_trajectory_021.jsonl
```

内部顺序：阻尼 lock → teach stop → `recordctl start --variable` →
`teach play`（参数自动读 `~/.config/panthera-wam/teach-cal.json` 的 kp/kd/fc/fv，
可用 `RECORD_FORMAL_KP/KD/FC/FV` 环境变量覆盖）→ 结束 lock（闭爪 10%）→
`recordctl stop` → `verify`。完成后按提示跑 `rezero`。

⚠ 回放需要方块摆在录制起始位置。`record-formal` 真机全流程尚未现场验收，
第一次执行时人必须在场、E-stop 可触达，并逐段确认（见 §9 未验证清单）。

变长契约：episode 窗口 = stop 前的全部公共对齐帧（无固定 901/900 要求；
`fixed_length.enabled=false`）。安全上限 `--max-duration-s 180` 到点自动收尾。

## 5. HF 上传

```bash
./deploy/lerobot-collect.sh hf-upload color-block-000011
```

等价于 `recordctl verify` + `panthera-hf-upload-episode`，endpoint 默认
`https://hf-mirror.com`（Pi 直连 huggingface.co 不可达）。只传含 `COMPLETE`
的原子 episode；输出中的 40 位 Hub revision 必须随实验记录（AutoDL 按 revision
下载不可变字节）。上传前先 `hf auth login`（命令复用现有凭据，不打印 token）。

## 6. 夹爪调紧工具（"J4 夹爪"的轴名说明）

数据是 7 轴定序：`joint_1 joint_2 joint_3 joint_4 joint_5 joint_6 gripper`。
**夹爪是第 7 轴 `gripper`（电机 J7）；J4 是腕俯仰关节**。调整夹爪请用：

```bash
python3 tools/adjust-gripper.py \
    ~/panthera-data/preview/color-block_021/replay_trajectory_021.jsonl \
    --pct 5 --dry-run          # 先看统计：每帧 gripper -0.1（全量程 2.0 的 5%）
python3 tools/adjust-gripper.py \
    ~/panthera-data/preview/color-block_021/replay_trajectory_021.jsonl \
    --pct 5                    # 写 <stem>.tighten5pct.jsonl，原文件备份 .bak
```

收紧 5% 全量程：开爪 1.8→1.7、闭爪 0.2→0.1（下限 0.0 截断）。调紧后把新文件
路径传给 `record-formal`。原始数据永不覆盖。

## 7. FastWAM 数据格式调研结论

`FastWAM/`（`/home/winbeau/Papers/ICLR2027-WAM-Reprojection/FastWAM`）实际读取的是
**LeRobotDataset v3 打包目录**，契约名 `panthera-fastwam-v1`（见
`FastWAM/src/fastwam/datasets/lerobot/lerobot_v3_adapter.py` 与
`FastWAM/tests/fixtures/panthera_lerobot_v3_minimal/`）：

```text
<dataset_dir>/
├── meta/info.json                     # fps=30、codebase v3.0、features、路径模板
├── data/chunk-*/file-*.parquet        # 每帧: index/episode_index/task_index/timestamp + features
├── videos/{video_key}/chunk-*/file-*.mp4   # 每 episode 每相机一段 h264 yuv420p 30fps
├── meta/episodes/chunk-*/file-*.parquet    # 每 episode: 起止 index + 视频 chunk/file/from_timestamp
├── meta/tasks.parquet                 # task_index → 任务文本
├── panthera-schema.json               # axes/units/camera_order/action_semantics/identity
├── panthera-package-manifest.json     # sha256 + lerobot 版本 + source commit/calibration
├── panthera-episode.json              # 源 episode 元数据
└── aux/{timestamps.jsonl, source/{calibration.json, sync_report.json, timestamp_quality.json}}
```

关键字段契约（FastWAM 严格校验）：

| 项目 | FastWAM 要求 | 我们当前采集 | 结论 |
|---|---|---|---|
| 7 轴顺序 | joint_1..6, gripper | 一致 | ✅ 直接 |
| 单位 | rad×6 + native_gripper_position | 一致 | ✅ 直接 |
| observation.state | float32 [7] 绝对位置 | samples.parquet state.position | ✅ 直接映射 |
| action | [7] = **q[t+1] 绝对位置**（30Hz） | `action_source=next_state_pseudo_action`，语义字符串完全一致 | ✅ 直接映射 |
| observation.velocity | float32 [7] | state.velocity | ✅ 直接映射 |
| 相机 | 每 episode 每相机一段 mp4（h264/yuv420p/30fps） | 每 tick 一张 JPEG/PNG | ⚠ 需重编码为 mp4 |
| 分辨率 | overhead 1080×1920 / wrist 480×640（训练 resize 224×224） | 原始分辨率已保留 | ✅ 可满足 |
| depth | sidecar only（不作为模型输入） | 可选 wrist_depth 已支持 | ✅ |
| 时间戳 | panthera.* 侧车字段 + aux/timestamps.jsonl | tick/sampled/host 时间戳全在 | ✅ 可导出 |

**回答"能否通过编码器等转换"：本体感知部分不需要任何编码器转换**——采集格式
已经就是 FastWAM 要的绝对关节角 + 原生夹爪位置 + q[t+1] 语义（两边的
`action_semantics` 字符串逐字一致，这是当初按同一契约设计的）。需要补的只有
**打包层**：把 episode 的逐帧图像编码成 mp4、生成 LeRobot v3 目录布局与
manifest——目前仓库里还没有这个 packager 脚本（`dataset_worker.py` 是旧式导出，
明确标注 `training_compatible_with_fastwam=False` 且缺图像）。开发 packager 时
以 fixture 为黄金样例，用 `FastWAM/scripts/validate_panthera_dataset.py` 做验收
（它会校验 hash、action==next_state、时间戳单调、视频解码形状）。归一化统计由
`FastWAM/scripts/prepare_panthera_assets.py` 在打包后生成。

## 8. 安全纪律（必读）

1. **高位形（工作0位定死锁）状态下绝不能重启 armd/收工**：150ms 固件看门狗，
   保持帧一停臂就坠落（已出过两次事故）。收工顺序永远是先 `zero-home` 回低位。
2. 所有运动命令带 `--confirm`；运动期间人在场、E-stop 可触达。
3. 开/闭爪由脚本做，动作指令只到"方块移到目标区上方"：开爪 90%（1.8）、
   闭爪 10%（0.2）。
4. 速度系数已提到 8×（8 rad/s / 16 rad/s²，env `*_LIMIT_SCALE=8`），比官方
   上限激进；任何异常声音/抖动/关节 mode 变 `0x0B` 立即 E-stop。
5. 连续两段录制之间：rezero 后臂已在工作0位（定死锁），可直接从
   `start-record` 继续，不必重复 gozero。
6. 在 Pi 上不要用 `uv run --directory armd`，也不要把裸 `uv sync --frozen`
   当作在线修复：前者会重建 `.venv`，后者会移除未写入锁文件的厂商
   `hightorque_robot` wheel。只在机械臂回到低位并进入维护窗口后运行
   `deploy/install-pi5.sh`；该脚本会同步环境并重新安装匹配 CPython/ARM64 的 wheel。

## 9. 已知问题与未验证清单

- **J7 开爪后嗡鸣 bug（用户暂缓）**：保存的工作零位 gripper=1.9735 超出物理
  极限（≈96%）。已做两层修复：开爪目标钳位 90%（1.8）+ 到位后持久锁存
  （`ae0c45e`）。若真机仍复现，下一步方案是"J1-J6 定死锁 + J7 零刚度"的同帧
  模式（不能简单停帧，会坠臂）。现象观察：`panthera state get` 看 J7
  pos/vel/torque。
- **fraction 显示 1.000998**：显示溢出 <0.1%，不影响 DONE 判定。
- **未现场验收**：`record-formal` 全流程（含真机 teach play 回放）、
  `recordctl --variable` 变长 episode、`lerobot-collect.sh` 六条命令的
  真机串联——第一次执行逐段确认。
- **teach play 参数**：自动读 `teach-cal.json`（kp=kd=0、fc/fv 按标定）；
  若回放跟踪偏差大，先调 fc/fv（`deploy/teach-cal.sh --Jx "0,0,±fc,0,0"`），
  注意 teach-cal.sh 会重启 armd——**改参数前先 zero-home**。

## 10. 文件索引

| 文件 | 作用 |
|---|---|
| `deploy/lerobot-collect.sh` | 六条一行命令 + record-formal + hf-upload 分发器 |
| `deploy/recordctl.sh` | collectord 定长/变长录制控制面（start/stop/status/verify） |
| `deploy/zero-home.sh` | 回初始0位 + 快速闭爪（收工/重启前必做） |
| `deploy/workzero-session.sh` | 旧的端到端引导脚本（分阶段交互版，语义与一行命令一致） |
| `tools/adjust-gripper.py` | 夹爪按百分比调紧工具 |
| `tools/preview-record.py` | preview 录制实现（MP4 + 轨迹 + manifest） |
| `docs/RECORDING_PLAYBOOK.md` | 定长双终端流程旧手册（固定 30s 契约部分已被变长模式替代） |
| `docs/FASTWAM_COLLECTION.md` | 采集/时间戳/原子提交/质量门契约 |
| `docs/FINAL_PLAN.md`（WZ-2 节） | work-zero 架构决策与真机修订记录 |
