# FastWAM collection contract

> 现场录制逐步操作见 **`docs/RECORDING_PLAYBOOK.md`**（2026-08-13 打通验证，照抄即可）。
> 本文定义所有权、时间戳、原子 staging 与硬门契约。

## Ownership and safety

- `HardwareLoop` remains the only owner of the robot backend and the only producer of measured-state sequence numbers.
- Each cycle performs `refresh_state()` and one seven-motor `read_all()`, then creates one immutable sample with a Pi-monotonic timestamp.
- `StateTap` is a bounded multi-reader ring. A reader within retention receives every sequence; overflow is explicit `DATA_LOSS` and invalidates the episode.
- `StreamState` remains a 100 Hz latest-value UI stream. Collection uses `StreamMeasuredState` and never polls the latest cache.
- Collection is read-only. `collectord` does not acquire a control lease and cannot send movement commands.

Teach recording now drains the same state tap from a separate reader thread; arbitrary recorder callbacks no longer execute in the 200 Hz hardware loop.

## Camera time and loss accounting

`CameraFrame` retains the legacy fields for WPF compatibility and additionally records:

- raw device timestamp, unit, and clock domain;
- host receive and host publish monotonic timestamps;
- optional estimated capture time in the Pi monotonic domain;
- timestamp source and quality;
- device frame number, frameset sequence, and stream instance ID.

D405 color and depth from one frameset share host-receive time and frameset sequence. `collectord` fits an affine device-clock-to-Pi mapping when at least three native timestamps are available and records drift/residual metrics.

The current C920e `v4l2-ctl` JPEG path does not expose `v4l2_buffer.timestamp`. It therefore reports `HOST_RECEIVE/HOST_OBSERVED` and does not fabricate a device timestamp. Exposure-offset thresholds remain disabled measurement candidates until a dequeue path provides the kernel buffer timestamp.

Preview uses the latest frame. Collection uses `StreamCollectedFrames`, backed by a separate bounded per-stream ring with explicit overflow and sequence accounting.

## Run collectord

Production collection roots require an approval marker named `.panthera-usb3-ssd.json`:

```json
{"approved": true, "device_class": "usb3_ssd"}
```

The marker is created only after the target mount, sustained write throughput, fsync latency, free-space reserve, and thermal behavior have been measured. The `--sim-allow-unapproved-root` flag is for tests only.

生产真机录制使用双终端控制脚本，不要手写长命令：

```bash
# 终端 A：启动定长录制并持续监看日志
./deploy/recordctl.sh start color-block-000004

# 终端 B：只做 teach lock/drag；动作完成后请求优雅结束
./deploy/teach-cal.sh lock
./deploy/teach-cal.sh drag
./deploy/teach-cal.sh lock
./deploy/recordctl.sh stop color-block-000004
```

定长契约为：30 s → 901 canonical ticks → 900 training frames。默认额外采集 5 s
对齐余量；graceful stop 时发布最后一个完整的 30 s 公共窗口，而不是采集开头窗口，
因此启动/建流延迟不会进入最终 episode。不会补帧、复用帧或发布不完整 episode。
`recordctl.sh status/watch/verify` 可在 SSH 断线后恢复观察。

底层调试命令仍可直接使用：

```bash
.venv/bin/collectord \
  --collection-root /mnt/panthera-ssd \
  --episode-id color-block-000001 \
  --task 'Move the red block from the start area to the target area.' \
  --operator operator-id \
  --panthera-commit <40-char-sha> \
  --calibration calibration.json \
  --identity dataset-identity.json \
  --fixed-duration-s 30 \
  --fixed-margin-s 5 \
  --capture-depth
```

`SIGUSR1` requests graceful finish; `SIGTERM` aborts and retains `FAILED.json`. Default endpoints
are Pi-local ports 50051/50052/50053. Camera roles and configured serials are checked before recording.

## Action-only window contract (work-zero)

每次任务会话严格拆分为 `gozero → settle → ACTION → commit → rezero`（见
`docs/WORKZERO_IMPLEMENTATION_PLAN.md` 与 `docs/JOINT_CONTROL.md` §7）：

- preview 与正式 episode 只记录 **ACTION 窗口**；gozero/rezero、启动准备、失败收尾
  与任务无关的回零动作永不进入训练 action；
- preview.json / episode manifest 携带 `motion_scope=task_action_only`、
  `gozero_excluded/rezero_excluded=true` 与 action_window 起止
  sequence/monotonic 字段（P1 已落 preview.json，正式 episode 在 P4 落地）；
- 901 canonical ticks / 900 training frames 只由 action 窗口生成；
  窗口裁剪必须发生在 canonical ticks 形成之前，或由 packager 明确按
  action window 裁剪后重建 901/900，不允许先混入回零帧再静默丢弃；
- 无 action_window 或字段缺失的旧 preview 标记 legacy/rejected，不回填为成功。

## Upload each complete episode to Hugging Face

The fixed interchange repository is the Hugging Face **dataset** repo
`winbeau/fastwam-lerobot`. Upload only an atomically published episode containing
`COMPLETE`; failed or temporary staging directories are rejected. The command creates one
portable tar archive plus SHA-256 and JSON manifests below
`staging/episodes/<episode_id>.*`:

```bash
HF_ENDPOINT=https://hf-mirror.com \
uv run --no-sync --package panthera-armd panthera-hf-upload-episode \
  /mnt/panthera-ssd/episodes/color-block-000001
```

Use `--kind smoke` for non-training diagnostics. The output reports the Hub revision; record
that 40-character revision with the experiment so AutoDL downloads immutable bytes. The
command uses the existing `hf auth login` credential without printing or copying the token.
Pi 5 currently requires the verified mirror endpoint because direct routing to
`huggingface.co:443` is unavailable.

## Hardware policy experiment tolerances

Hardware acceptance uses a 3 cm tool-point endpoint tolerance (`PANTHERA_POLICY_ENDPOINT_TOLERANCE_M=0.03`). Test moves must be large enough for the Panthera actuators and cameras to resolve: prefer a conservatively planned Cartesian displacement around 10 cm, or at least 10 degrees on a safely selected joint, rather than millimetre-scale or few-degree probes. The larger test stimulus does not relax commanded waypoint soft limits, table/base/camera exclusion geometry, velocity/acceleration/jerk gates, tracking cancellation, or E-Stop. `GetPolicyAcceptance` reports the terminal tool-point Euclidean error and the configured 3 cm threshold for each policy execution. Hardware policy remains disabled until `PANTHERA_POLICY_CAMERA_BOXES_JSON` contains at least one field-calibrated camera/support exclusion box; provisional empty geometry is not accepted.

A separate startup measurement tolerance (`PANTHERA_POLICY_START_MEASUREMENT_TOLERANCE=0.01`) may admit a measured anchor marginally outside a configured software bound, such as the observed gripper zero bias. Every commanded waypoint remains inside the original limits, and the sampled trajectory may only recover from that measured anchor toward the legal interval; it cannot move farther outside.

Hardware execution also requires a deployment allow-list (`PANTHERA_POLICY_ASSET_ALLOW_LIST`) that exactly matches the checkpoint, normalization-statistics, and schema SHA-256 triple. Digest syntax alone is never an authorization. Each hardware request must additionally carry a Pi-issued confirmation token bound to that request/session; the token is short-lived and consumed once. The token is not available from gRPC/Tailscale: an operator logged into the Pi must run `panthera-policy-confirm --request-id <id> --session-id <id> --confirm` in a local interactive TTY. E-Stop, force/release, and watchdog lease expiry revoke every pending token.

## Atomic staging

A recording is first written below the same filesystem as:

```text
episodes/.<episode_id>.tmp-<uuid>/
├── episode.json
├── samples.parquet
├── overhead/
├── wrist_rgb/
├── wrist_depth/          # optional
├── sync_report.json
├── timestamp_quality.json
├── calibration.json
└── COMPLETE              # written only after all gates pass
```

Canonical ticks are generated at rational 30 Hz without accumulated truncation. In fixed mode,
`fixed_length.canonical_ticks` is enforced before publication; 30 s means 901 ticks because both
endpoints are represented. State is linearly interpolated from the 200 Hz tap. Camera frames are
selected by estimated capture time, or explicitly degraded host receive time, without silent frame
reuse. Camera/state motion correlation evaluates offsets `[-2,-1,0,1,2]`; insufficient motion
produces a null offset rather than a fabricated zero.

After validation, files and directories are fsynced, `COMPLETE` is written, and the temporary directory is atomically renamed. Failed capture or quality checks retain `FAILED.json` and never receive `COMPLETE`.

## Frozen hard gates

An episode is rejected when any of these are nonzero or incomplete:

- timestamp regressions;
- unexplained source sequence gaps;
- duplicate canonical camera selections;
- state/camera ring overflow;
- missing required canonical state or RGB frame;
- timestamp source/quality coverage below 100%;
- malformed seven-axis state, image, depth, identity, calibration, or report totals.

The 99% valid-tick target and state/camera p95/max offset targets are recorded as disabled candidates until real hardware timing evidence is collected.
