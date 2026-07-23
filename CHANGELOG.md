# Changelog

All notable changes to Panthera-WAM are documented in this file.

## [Unreleased]

## [2.2.13] - 2026-07-23

### Added

- Added an independent Raspberry Pi 5 C920e overhead-camera service on port 50053 using the stable `/home/winbeau/camera-devices/c920e` alias and native V4L2 MJPEG capture.
- Extended the camera contract with wrist/overhead roles, JPEG frames and monotonic capture timestamps, with matching Python and WPF clients.
- Added a top-level C920e status indicator and a live overhead preview to the WPF terminal.

### Changed

- Extended SSH deployment and persistent forwarding with `127.0.0.1:50048 → remote:50053`, while retaining the existing armd and D405 endpoints.
- Reworked the control page into a linked square camera column: CAD top view, C920e overhead view and D405 wrist view resize together through the native WPF `GridSplitter`.
- Isolated wrist color/depth and overhead color into independent latest-frame pumps so one camera stream can reconnect without stopping the others.

### Safety and validation

- Verified the real C920e on Raspberry Pi 5 at 1920×1080 MJPEG and approximately 30 fps, including a valid snapshot and 300 consecutive frames.
- Verified WPF through a real SSH tunnel against the Pi C920e while armd remained in simulation; no control lease, motor enable or motion command was issued.
- WPF Release build completed with zero warnings; all 44 unit tests and 9 UI tests passed.

## [2.2.12] - 2026-07-22

### Added

- Added a unified shadowed SSH deployment progress dialog that visualizes all six probe, detection, startup and health-check stages in real time.
- Added per-stage waiting, running, success and failure states, plus in-dialog cancellation and final restart confirmation.

### Fixed

- Prevented the native Panthera SDK from entering a segmentation-fault restart loop when the communication board is powered off or `/dev/ttyACM*` is absent.
- Changed remote startup verification from a single instantaneous `systemctl is-active` check to three consecutive healthy samples of both services and ports 50051/50052.
- Added actionable service status and journal diagnostics when the remote backend cannot remain stable.

### Changed

- Replaced the system result message boxes with the same borderless, shadowed modal language used by the SSH connection dialog.

### Safety and validation

- Re-ran the complete Windows-to-SSH-to-Raspberry-Pi startup flow on the real Pi 5 without acquiring control, enabling motors or sending motion commands.
- Verified all seven motors were detected, both services stayed active, ports 50051/50052 remained available and `armd` reported zero restarts.
- Raspberry Pi passed all 91 `armd` tests; WPF Release build completed with zero warnings and all 41 unit tests passed.

## [2.2.11] - 2026-07-22

### Fixed

- Normalized remotely executed shell scripts to LF before Base64 encoding so Raspberry Pi `/bin/sh` (`dash`) no longer rejects Windows CRLF as `set: Illegal option -`.
- Added a cross-platform line-ending regression covering the SSH probe script.

### Changed

- Added a soft outer shadow and transparent spacing around the SSH deployment dialog so it reads as a floating modal instead of embedded content.

## [2.2.10] - 2026-07-22

### Fixed

- Prevented the SSH dialog from freezing when a third-party Windows mDNS namespace provider blocks synchronously during `.local` Raspberry Pi discovery.
- Moved all SSH candidate discovery startup off the WPF dispatcher and added a ten-second overall timeout.
- Replaced in-process mDNS resolution with a bounded, killable child-process probe.
- Added a UI regression that simulates synchronous discovery startup and requires the dialog dispatcher to remain responsive.

## [2.2.9] - 2026-07-22

### Added

- Added editable host/IP and username selectors populated from the previous connection, OpenSSH configuration, WSL instances, and discoverable Raspberry Pi Tailscale/mDNS nodes.

### Fixed

- Fixed the SSH deployment button appearing unresponsive because the modal Fluent backdrop initialization threw before the window was shown.
- Added a UI acceptance regression that invokes the button and requires the SSH dialog to reach its loaded state.

### Changed

- Replaced the SSH dialog's system title bar with a compact borderless panel and a chromeless top-right close button.
- Removed the two explanatory paragraphs and the redundant bottom cancel button.
- Removed the decorative icon from the main SSH deployment button.

## [2.2.8] - 2026-07-22

### Added

- Migrated the primary hardware-control host to Raspberry Pi 5 ARM64 while retaining the WSL2 compatibility path and direct WPF remote mode.
- Added the WPF SSH deployment wizard, automatic Pi/WSL architecture and repository detection, existing-service startup, persistent dual gRPC port forwarding and safe application restart.
- Added Raspberry Pi 5 `uv`/vendor-submodule deployment support for the ARM64 motor SDK and vendored RealSense RSUSB backend.

### Fixed

- Fixed J2/J3 positive jog commands using stale asymmetric joint-state data, which could produce a large first-step command under load.
- Pinned the Raspberry Pi service environment to the repository-managed `uv` interpreter and validated the real-hardware handover path.
- Aligned trajectory dispatch with the verified official SDK behavior and smoothed the built-in demonstration sequence.

### Safety and validation

- WPF Release build passes with zero warnings and zero errors, 33 unit tests and eight isolated FlaUI acceptance tests.
- The SSH wizard never acquires control, enables motors or sends motion commands; it does not clone repositories or install remote dependencies.
- Raspberry Pi real-hardware acceptance verified all seven motors, lease acquire/release, a bounded J1 jog, EStop latch/reset and zero-torque recovery.

### Earlier v2 additions included in this release

- Forked and pinned RealSense SDK 2.0 v2.58.1 as `vendor/librealsense`.
- Added vendored librealsense RSUSB source build and an isolated WSL `camerad` that owns D405 acquisition without disturbing the arm control loop.
- Added CameraService status, snapshot and frame streaming on the dedicated `camerad:50052` endpoint alongside `armd:50051` in the same Linux backend.
- Added `panthera camera status`, `snapshot` and `stream`; WPF attaches both devices to WSL and remains a pure gRPC visualization terminal.
- Added MIT joint/gripper control, six dynamics diagnostics and damped-pseudoinverse Cartesian jog.
- Added cancellable multi-waypoint septic trajectories and drag-teach JSONL recording with MIT/POS-VEL playback.
- Added independent `dataset.proto` jobs and an isolated official LeRobotDataset v3 exporter.
- Added WPF dual TCP bridges plus RGB8/Z16 latest-frame video panels for the dedicated camera endpoint.
- Added an executable 42-method SDK capability audit, including verified inherited `set_stop` and zero-reset signatures.

## [1.0.0] - 2026-07-18

### Added

- `armd` 200Hz single-owner hardware daemon with gRPC, lease control, watchdog, software limits,
  EStop preemption, non-blocking motion execution and simulation mode.
- `panthera-cli` v1 command surface with 27 commands for daemon/control/state/calibration, joints,
  gripper, Cartesian motion, kinematics and safety checks.
- .NET 9 WPF cockpit with WSL TCP bridge, System/Light/Dark/HighContrast themes, MoveJ/MoveL,
  gripper, EStop, cancellation and twelve keyboard-accessible Jog controls.
- Single-file Inno Setup `win-x64` installer produced and attached to Releases by GitHub Actions.
- MIT license and public SDK submodule pin.

### Safety and validation

- Verified 7/7 motors on real hardware with CANboard v4.8.6 and motor firmware v4.7.3.
- Verified gripper upper/lower software-limit rejection without physical motion.
- Verified MoveL `DONE` and smooth `CANCELLED` terminal states with monotonic progress.
- Verified whole-arm zeroing is non-persistent and causes no physical motion; original coordinates
  recover after a controller power cycle.
- CI passes 57 Python tests, seven .NET unit tests, four themed FlaUI launches/screenshots and a
  complete Tab-focus cycle across all 22 v1 controls.

### Notes

- Real-hardware commands remain subject to per-action operator confirmation. CI and daily
  development use `armd --sim` only.
- v2 work (dynamics, waypoint trajectories, teach/record/playback, D405 and LeRobot export) remains
  intentionally out of scope for this release.
