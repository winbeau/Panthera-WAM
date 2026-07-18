# Changelog

All notable changes to Panthera-WAM are documented in this file.

## [1.0.0] - 2026-07-18

### Added

- `armd` 200Hz single-owner hardware daemon with gRPC, lease control, watchdog, software limits,
  EStop preemption, non-blocking motion execution and simulation mode.
- `panthera-cli` v1 command surface with 27 commands for daemon/control/state/calibration, joints,
  gripper, Cartesian motion, kinematics and safety checks.
- .NET 9 WPF cockpit with WSL TCP bridge, System/Light/Dark/HighContrast themes, MoveJ/MoveL,
  gripper, EStop, cancellation and twelve keyboard-accessible Jog controls.
- Self-contained `win-x64` terminal package produced by GitHub Actions.
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
