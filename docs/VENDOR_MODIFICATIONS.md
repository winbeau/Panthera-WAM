# Vendor 修改与固定版本审计

> 审计日期：2026-07-22
>
> 适用范围：主仓库 `.gitmodules` 中的全部 vendor。架构与安全决策仍以
> [`FINAL_PLAN.md`](FINAL_PLAN.md) 为唯一权威来源。

本文记录各 vendor 的来源、主仓库固定提交、相对上游的实际修改，以及修改应当落在哪个仓库。
目的不是复制 vendor 的 changelog，而是避免把“使用 fork”“主仓库适配”和“fork 私有补丁”混为一谈。

## 1. 当前结论

| 路径 | 仓库性质 | 主仓库固定提交 | 相对上游的 fork 私有修改 | Panthera-WAM 的实际依赖 |
|---|---|---|---|---|
| `vendor/Panthera-HT_SDK` | `winbeau` 对官方 SDK 的公开 fork | `08db76e2a3cb519b298744f06ab7dcb99a6d87e7` | **0**；审计时 fork `main` 与上游 `main` 完全一致 | Python/C++ SDK、URDF、参数文件及按架构提供的 wheel |
| `vendor/librealsense` | `winbeau` 对 RealSense SDK 的公开 fork | `bf2778061d5dd29776e9aca8765f75852671760b`（上游 `v2.58.1`） | **0**；固定提交就是上游发布提交 | D405、udev 规则、RSUSB/libusb 源码构建和 `pyrealsense2` |
| `vendor/Panthera-HT-TriView` | `winbeau` 自有仓库，**不是 GitHub fork** | `b87328da7f1ed9e907a388ea25dff69701426ca4` | 不适用；该仓库本身就是项目维护的三视图实现 | WPF 打包时使用的 HTML、Three.js、轻量网格、精确 GLB 和第三方许可文件 |

因此，当前主仓库没有需要补推的 vendor 源码修改，也不需要更新任何 gitlink。fork 的作用是提供可控的
公开维护入口；只有将来确实修改 vendor 后，才应先在对应仓库提交，再更新本仓库的 submodule 指针。

## 2. Panthera-HT SDK

- Fork：[`winbeau/Panthera-HT_SDK`](https://github.com/winbeau/Panthera-HT_SDK)
- 上游：[`HighTorque-Robotics/Panthera-HT_SDK`](https://github.com/HighTorque-Robotics/Panthera-HT_SDK)
- 固定提交：[`08db76e2`](https://github.com/winbeau/Panthera-HT_SDK/commit/08db76e2a3cb519b298744f06ab7dcb99a6d87e7)
- 上游对比：[`main...winbeau:main`](https://github.com/HighTorque-Robotics/Panthera-HT_SDK/compare/main...winbeau:main)

### 2.1 fork 修改

审计时 GitHub compare 返回 `identical`，即 `ahead_by=0`、`behind_by=0`。当前固定提交没有
`winbeau` 私有补丁。仓库内已有的 ARM64 wheel 也是该提交原有内容，不应写成 Panthera-WAM 的 fork 修改：

- ARM64：Python 3.9/3.10/3.11/3.12，包版本 `1.0.0`；
- x86_64：Python 3.9/3.10/3.11/3.12，包版本 `1.2.0`。

### 2.2 主仓库适配

Panthera-WAM 不直接修改 SDK 源码。适配发生在 `armd`：

- `RealBackend` 从 `vendor/Panthera-HT_SDK` 加载 SDK、Follower 参数和 wheel；
- 阻塞式 `iswait=True`、`moveL()`、`Recorder.play()` 不进入 HardwareLoop；
- 轨迹、取消、watchdog、软限位和 lease 由主仓库实现；
- SDK 的公开规划/控制原语由 armd 以非阻塞状态机组合。

这类逻辑属于控制服务的安全边界，不应为了“少写一层”而下沉到 vendor fork。

## 3. librealsense

- Fork：[`winbeau/librealsense`](https://github.com/winbeau/librealsense)
- 上游：[`realsenseai/librealsense`](https://github.com/realsenseai/librealsense)
- 固定提交：[`bf277806`](https://github.com/realsenseai/librealsense/commit/bf2778061d5dd29776e9aca8765f75852671760b)
- 对应上游版本：[`v2.58.1`](https://github.com/realsenseai/librealsense/releases/tag/v2.58.1)

### 3.1 fork 修改

当前 gitlink 直接固定到上游 `v2.58.1` 的提交，内容没有 fork 私有修改。fork 默认分支 `master`
不是主仓库的版本依据；审计时它相对上游最新 `master` 已落后 618 个提交。更新时必须按发布版本和 D405
回归结果选择提交，不能仅执行 fork 同步后就让 submodule 跟随默认分支。

### 3.2 主仓库适配

D405 的 WSL2 兼容性改动位于主仓库，而不是 librealsense fork：

- `deploy/build-realsense-wsl.sh` 从固定源码构建；
- 使用 `FORCE_RSUSB_BACKEND=ON`，绕过 WSL 下不稳定的内核 V4L2 路径；
- 开启 Python bindings，并把生成的 `pyrealsense2` 安装到 uv 环境；
- 使用 vendor 中的 `99-realsense-libusb.rules` 配置设备权限；
- `camerad` 独占相机，WPF/CLI 只通过 CameraService 访问。

### 3.3 Windows 脏标记说明

librealsense 上游将以下 Noble patch 文件记录为符号链接：

- `scripts/realsense-camera-formats-noble-master.patch`
- `scripts/realsense-metadata-noble-master.patch`

同一工作树被 WSL Git 与 Git for Windows 交替读取时，Windows 可能将它们显示为 reparse point，并让父仓库出现
小写的 `m vendor/librealsense`。这不代表 gitlink 改变。判断是否应提交前应在 Linux/WSL 中执行：

```bash
git -C vendor/librealsense status --short
git submodule status --recursive
```

只有 submodule `HEAD` 确实改变并且对应 fork 已存在可追溯提交时，才能更新主仓库 gitlink。

## 4. Panthera-HT TriView

- 仓库：[`winbeau/Panthera-HT-TriView`](https://github.com/winbeau/Panthera-HT-TriView)
- 固定提交：[`b87328da`](https://github.com/winbeau/Panthera-HT-TriView/commit/b87328da7f1ed9e907a388ea25dff69701426ca4)
- GitHub 属性：`isFork=false`，无 parent 仓库。

### 4.1 项目维护的修改

当前固定点包含三笔提交：

1. `c82ef87 feat: 初始化 Panthera 三视图资源仓库`
   - 纳入官方 follower URDF 与 `base_link/link1-link6` STL；
   - 建立 Blender 六轴层级、夹爪拆分、GLB/BLEND 导出；
   - 建立无框架 Three.js 正交三视图、轻量网页网格与预览资产；
   - 增加 HighTorque 与 Three.js 的第三方许可和 notices。
2. `39f4c11 docs: 补充 WSL localhost 启动命令`
   - 说明从 WSL 启动本地服务并由 Windows `localhost` 访问的方法。
3. `b87328d fix: 自动避让三视图预览端口冲突`
   - 增加 Python 静态服务器；默认端口被占用时自动选择后续空闲端口。

### 4.2 主仓库使用方式

WPF 项目在构建时直接从 submodule 链接下列内容，而不是复制后再修改：

- `panthera-six-axis-three-view.html`；
- `three-r160.min.js` 与 `panthera-ht-meshes.min.js`；
- 精确 CAD GLB；
- `THIRD_PARTY_NOTICES.md` 与 `LICENSES/*.txt`。

`docs/blender/` 与 `docs/mockups/` 中的文件用于主仓库设计记录和开发预览；WPF 发布物的运行时来源仍以
`vendor/Panthera-HT-TriView` 为准。

TriView 自有代码目前没有单独声明开源许可证。内部使用不受影响；若要允许第三方复用或再发布，应先在
TriView 仓库补充许可证，同时继续保留 HighTorque CAD/URDF 与 Three.js 的第三方 notices。

## 5. 修改与升级流程

### 5.1 修改 vendor

```text
对应 vendor fork/仓库建分支
  -> 查上游 Issue/PR 与已有解决方案
  -> 在 vendor 仓库修改、测试、commit、push
  -> 主仓库更新 submodule gitlink
  -> 主仓库记录原因、固定 SHA 与验证结果
```

主仓库不得把 vendor 源文件复制到业务目录后静默修补。确需等价重写 SDK 私有流程时，必须遵守
`FINAL_PLAN.md` 的对拍验证要求。

### 5.2 升级前检查

```bash
git submodule status --recursive
git -C vendor/Panthera-HT_SDK log -1 --oneline
git -C vendor/librealsense log -1 --oneline
git -C vendor/Panthera-HT-TriView log -1 --oneline
```

升级还必须分别验证：

- SDK：42 项能力审计、N1/N4/N5 防护、仿真回归；涉及控制语义时再安排真机验收；
- librealsense：D405 枚举、RSUSB 双流、冷重连、300 帧无超时；
- TriView：WPF Release 构建、资源打包、三视图加载、主题与 UI 验收。

## 6. 本次审计证据

- 三个仓库的公开 fork/parent 元数据与默认分支通过 GitHub API 核对；
- 三个 `issues?state=all` 均为空，当前 fork/自有仓库没有公开 Issue 或 PR；
- SDK fork 与上游 compare 为 `identical`；
- librealsense 固定 SHA 与上游 `v2.58.1` SHA 完全相同；
- TriView 的三笔提交与主仓库 WPF `.csproj` 资源引用逐项核对。
