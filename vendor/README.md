# Vendor Dependencies

External SDKs and project-owned reusable assets are tracked as git submodules rather than copied into this repository.

Pinned revisions, fork-only deltas, main-repository adaptations, and update checks are audited in
[`docs/VENDOR_MODIFICATIONS.md`](../docs/VENDOR_MODIFICATIONS.md).

## Panthera-HT SDK

- Fork: `https://github.com/winbeau/Panthera-HT_SDK`
- Upstream: `https://github.com/HighTorque-Robotics/Panthera-HT_SDK`
- Tracked branch: `main`
- Sync fork: `gh repo sync winbeau/Panthera-HT_SDK --source HighTorque-Robotics/Panthera-HT_SDK`

## RealSense SDK 2.0

- Fork: `https://github.com/winbeau/librealsense`
- Upstream: `https://github.com/realsenseai/librealsense`
- Pinned release: `v2.58.1` (`bf2778061d5dd29776e9aca8765f75852671760b`)
- License: Apache-2.0
- Sync fork: `gh repo sync winbeau/librealsense --source realsenseai/librealsense`

## Panthera-HT TriView

- Repository: `https://github.com/winbeau/Panthera-HT-TriView`
- Tracked branch: `main`
- Purpose: exact CAD three-view assets and rendering runtime reused by the WPF v2 terminal

Initialize all dependencies with `git submodule update --init --recursive`. SDK and visualization changes belong in the
corresponding fork first; commit an updated submodule gitlink separately in this repository.
