# Panthera-HT WPF 控制终端 —— 详细设计计划

> **v2.1 紧凑 Fluent 重构（2026-07-19）**：控制页调整为“宽 CAD 三视图 + 296px 紧凑控制轨”。删除左侧重复的 6 轴状态卡和 TCP 卡；J1–J6 当前位置与真实软限位进度统一迁入右侧 hold-to-run Jog 卡，夹爪状态保留在同一控制轨。三视图右列改为“俯视图 + D405 RGB 实时画面”，TCP XYZ 以红/绿/蓝轴向胶囊常驻 CAD 标题栏。底部 MoveJ/MoveL 不再使用 Tab，而是左右并列显示 12 个输入框、独立时长滑杆、运动空间说明、执行与取消按钮。CAD 宽度 ≥980px 时采用侧视/主视大列与俯视/RGB 窄列，低于 980px 自动切换 2×2；固定基座镜头为侧视和主视预留完整取景。窗口声明 PerMonitorV2 DPI 感知，并针对 2560×1600、150% Windows 缩放采用宽而较矮的自适应初始尺寸；UI 最小缩放锁定 90%。浅色/深色、1600×960、1240×800 以及 2560×1600@150% 均纳入实际 WPF 验收，功能与键盘可达性不减少。

> **v2 视觉定稿（2026-07-19）**：UI 采用用户选定的 A 版控制台，以双 Tab 承载“控制与三视图”和“D405 与数据采集”。控制页以精确 CAD 三视图为主视觉，6 轴/TCP 实时状态并入三视图内部，右侧承载夹爪、Jog 与控制权；数据页承载 RGB/深度流、示教录制、轨迹回放和 LeRobot 导出。`docs/mockups/mockup-C-fluent-cockpit.html` 仅保留为 WPF v1 历史基线；技术架构（MVVM/gRPC/主题系统）继续沿用。

> **Pi 5 直连迁移（2026-07-22）**：WPF 新增 `Remote` 后端模式，直接访问 Pi 的
> `armd:50051` 与 `camerad:50052`；此模式不启动 WSL TCP bridge，也不执行 usbipd/WSL
> 环境引导。另增 `SshRemote` 模式：用户显式填写 OpenSSH 基础信息后，WPF 自动识别
> Pi/WSL 架构、已部署仓库和启动入口，并通过 localhost SSH 隧道连接两个 gRPC 服务。
> 本文后续 WSL 引导内容保留为 `WslBridge` 兼容模式的历史设计。

> **双相机设备约定（2026-07-23）**：固定俯视画面来自 Logitech C920e，腕部
> RGB/深度来自 D405（序列号 `251323070051`）。Pi 5 上 V4L2/OpenCV 只能使用
> `/home/winbeau/camera-devices/` 的 udev 稳定别名，`pyrealsense2` 按序列号选取
> D405；禁止把 `/dev/videoN` 写入 WPF 配置或远程启动脚本。详见
> `docs/CAMERA_DEVICES.md`。

> 范围声明：本文档只覆盖 **客户端**（Windows WPF 可视化终端）一侧的设计。当前主路径是
> 六轴 Panthera-HT 与 D405 由同一 Raspberry Pi 5 后端控制，`armd:50051` 与
> `camerad:50052` 分进程独占硬件，WPF 分别连接两个 gRPC 端点。

---

## 0. 客户端在整体架构中的位置

```
┌─────────────────────────────┐      gRPC (Grpc.Net.Client)       ┌──────────────────────────────┐
│ Windows 宿主                 │  Pi IP:50051 / :50052              │ Raspberry Pi 5                │
│ ┌─────────────────────┐     │                                   │ ┌──────────────────────────┐ │
│ │ WPF 可视化终端       │◄────┼───────────────────────────────────┼─┤ armd → Panthera-HT       │ │
│ │ panthera-cli 也可并存 │     │                                   │ ├──────────────────────────┤ │
│ └─────────────────────┘     │                                   │ │ camerad → RealSense D405 │ │
└─────────────────────────────┘                                   │ └──────────────────────────┘ │
                                                                    └──────────────────────────────┘
```

WPF 终端与 `panthera-cli` 都分别连接机械臂和相机服务；只有 `armd:50051` 的
机械臂控制 RPC 使用 `AcquireControl` 互斥语义，`camerad:50052` 是只读采集端点。

---

## 1. 技术选型

### 1.1 UI 框架与主题：.NET 9 WPF 原生 Fluent 主题（首选）

**结论：采用 .NET 9 WPF 内置 Fluent 主题 + `ThemeMode`（System/Light/Dark）三态，不引入第三方主题库（如 WPF-UI / MahApps.Metro / MaterialDesignInXaml），也不手写全套 ResourceDictionary 皮肤系统。**

关键 API 事实（已用 context7 核对 dotnet/wpf 官方文档）：

- `Application.Current.ThemeMode` / `Window.ThemeMode` 是 .NET 9 引入的 **实验性 API**，取值 `Light` / `Dark` / `System` / `None`；`None` 会回退到经典 Aero2 主题而非 Fluent。代码里访问该属性需要压制 `WPF0001` 实验性诊断警告（项目级 `<NoWarn>` 或 `#pragma warning disable WPF0001`）——这是技术选型阶段就要写进 csproj 的已知事项，避免后续被当成"警告没处理"的技术债。
- Window 级 `ThemeMode` 优先于 Application 级，二者语义类似 CSS 的层叠覆盖，可用于"日志区强制暗色"这类局部例外（v1 不需要，但架构上留了口子）。
- Fluent 主题自带 `Light.xaml` / `Dark.xaml` / `HC.xaml`（高对比）三套 ResourceDictionary，`SystemAccentColor` 等强调色资源随 Windows 个性化设置自动联动；**操作系统开启高对比主题时，Fluent 会自动切到 HC 变体**，这意味着第 4 节的可访问性要求（高对比）大部分是"选对了主题就免费获得"，而不需要我们自己再实现一套 HC 配色。
- 前提：项目须 `TargetFramework=net9.0-windows`，`UseWPF=true`；Fluent 是 SDK 自带能力，不需要额外 NuGet 包。

**取舍对比（Fluent ThemeMode vs 自定义 ResourceDictionary 换肤方案）：**

| 维度 | .NET 9 Fluent + ThemeMode（选定） | 自定义 ResourceDictionary 换肤 |
|---|---|---|
| 开发成本 | 低：三态切换是框架属性赋值 | 高：需自建 Light/Dark/HC 三套字典 + 运行时 `MergedDictionaries` 热替换逻辑 |
| 与 Windows 系统主题跟随 | 原生支持 `System` 模式，跟随 `AppsUseLightTheme` 注册表/系统事件 | 需自己监听 `SystemParameters`/`UserPreferenceChanged` 事件并手动重载字典 |
| 高对比可访问性 | 框架自动提供 HC 变体，且与系统高对比主题联动 | 需要自己设计并测试第三套配色，工作量不小 |
| 视觉现代感（工控/示教器场景） | Fluent 观感新、贴近 Win11 原生控件，示教器长期使用不违和 | 依赖设计投入，风险是做成"半吊子皮肤" |
| 成熟度/风险 | 标记为 **实验性**（WPF0001），.NET 9 时间点 API 面还可能小幅调整 | 无实验性标记，但本质是重新发明轮子 |
| 自定义控件（关节仪表等）适配 | 只要控件用 `DynamicResource` 引用 Fluent 语义色板（如 `AccentFillColorDefaultBrush`），三态与 HC 全部免费获得 | 自定义控件要跟着三套字典各写一遍配色 |
| 第三方库依赖 | 零依赖，SDK 自带 | 视方案而定，MahApps/WPF-UI 等会引入外部包升级风险 |

**决策理由**：本项目是内部工控/机械臂示教终端，不需要品牌化换肤，只需要"能跟系统、能高对比、值班室大屏/暗光环境都能看清"。Fluent 原生方案用最小成本满足这些要求，且不牵扯额外 NuGet 依赖的供应链风险（工控软件对依赖树是敏感的）。唯一代价是接受 `ThemeMode` 目前是实验性 API —— 计划里记为已知风险项，跟踪 .NET 9 后续补丁/·.NET 10 转正情况（见第 7 节假设与风险）。

自定义控件（6 个关节仪表、Jog 按钮、EStop 按钮）**不使用**任何第三方图表/仪表库（如 LiveCharts2、ScottPlot、Syncfusion 等），而是基于 WPF 原生 `Path` + `ArcSegment` 手写轻量 `UserControl`：
- 理由：6 个仪表的视觉需求很简单（弧形刻度 + 指针/填充弧 + 数值文本 + 限位/故障色态），用重型图表库是杀鸡用牛刀，还会把 Fluent 主题联动这件事变复杂（第三方库有自己的配色系统，要额外做一层适配）；原生 `Path` 方案能直接用 `DynamicResource` 挂 Fluent 语义色，三态主题免费生效。
- 代价：需要自己写角度→弧长的几何计算（`ArcSegment` 的 `Point`/`IsLargeArc`/`SweepDirection`），工作量可控（预计每个仪表控件 <150 行 XAML+代码，且 6 个关节复用同一个 `UserControl`）。

### 1.2 其余技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| MVVM 框架 | `CommunityToolkit.Mvvm`（`ObservableObject` / `[ObservableProperty]` / `[RelayCommand]` / `IMessenger`） | 官方维护、Source Generator 减少样板代码，与 .NET 9 完全兼容 |
| DI/宿主 | `Microsoft.Extensions.Hosting`（`HostBuilder`）+ `Microsoft.Extensions.DependencyInjection` | WPF `App.xaml.cs` 里起一个 Generic Host，把 gRPC 后台流服务注册为 `IHostedService`，与 ViewModel/Service 统一走 DI 容器，避免 `new` 到处散落 |
| gRPC 客户端 | `Grpc.Net.Client` + `Google.Protobuf` + `Grpc.Tools`（生成 C# stub） | proto 文件与 armd 服务端共享同一份 `.proto`（建议单独一个 `panthera.proto` 包被两侧引用，避免契约漂移） |
| 重试/弹性 | `Grpc.Net.Client` 内建 retry（`ServiceConfig` + `MethodConfig.RetryPolicy`）为主，必要时叠加轻量退避逻辑 | 不引入完整 Polly，除非后续发现内建重试不够灵活 |
| 配置持久化 | 单个 JSON 文件存于 `%LOCALAPPDATA%\Panthera\terminal-settings.json` | 存储：armd 端点地址、主题偏好（System/Light/Dark）、Jog 步进档位、上次控制台窗口位置等 |
| 日志 | `Microsoft.Extensions.Logging` + 自建 `InMemoryLogSink`（供日志区 UI 绑定）+ 可选文件 Sink（滚动日志，便于事后复盘故障） | UI 日志区订阅同一个 `ILoggerProvider`，做到"看到的就是记下来的" |
| 测试 | `xUnit` + `Moq`/手写 fake `ArmdClient`，覆盖 ViewModel 状态机与错误映射逻辑 | gRPC 层通过接口抽象（`IArmdClient`）便于打桩，不依赖真实 armd 跑单测 |

---

## 2. 项目结构

### 2.1 解决方案分层

```
Panthera.Terminal.sln
├─ src/
│  ├─ Panthera.Terminal.App/            # WPF 可执行项目：App.xaml, MainWindow, 页面 XAML, 资源字典
│  ├─ Panthera.Terminal.Core/           # 平台无关：ViewModels, Models, 状态机, 错误映射, Messenger 消息定义
│  ├─ Panthera.Terminal.Grpc/           # gRPC 客户端封装：IArmdClient 接口 + 生成的 stub + Channel 管理
│  ├─ Panthera.Terminal.Controls/       # 自定义 UserControl：JointGauge, JogButton, EStopButton, StatusChip
│  ├─ Panthera.Terminal.Contracts/      # .proto 文件 + 生成代码的落地项目（供 Grpc 层引用，也可与 armd 共享同一份 proto 源）
│  └─ Panthera.Terminal.Settings/       # 配置读写、主题偏好持久化
└─ tests/
   └─ Panthera.Terminal.Tests/         # ViewModel/状态机/错误映射的单元测试（xUnit）
```

拆分理由：`Core` 不引用 `System.Windows.*`，保证 ViewModel 和状态机逻辑可以脱离 WPF 跑单测；`Grpc` 层对上只暴露 `IArmdClient` 接口（`Task<AcquireControlResult> AcquireControlAsync()`、`IAsyncEnumerable<JointState> StreamJointStateAsync()`、`Task JogAsync(...)`、`Task<MoveResult> MoveJAsync(...)` 等），ViewModel 完全不感知 gRPC/Channel 细节，方便测试期用 fake 实现替换。

### 2.2 MVVM 组织（CommunityToolkit.Mvvm）

- 每个页面区域一个 ViewModel，`MainWindowViewModel` 作为壳（Shell），聚合子 ViewModel：
  - `ConnectionBarViewModel`（顶栏：连接状态/控制权/Enable/EStop）
  - `JointMonitorViewModel`（6 关节仪表，持有 6 个 `JointGaugeViewModel`）
  - `JogPanelViewModel`（点动面板）
  - `CartesianPanelViewModel`（笛卡尔面板）
  - `LogPanelViewModel`（日志区）
- 跨 ViewModel 通信一律走 `CommunityToolkit.Mvvm.Messaging.IMessenger`，不做 ViewModel 之间的直接引用。典型消息：
  - `ConnectionStateChangedMessage`（连接状态变化 → 所有面板据此置灰/解禁）
  - `ControlOwnershipChangedMessage`（控制权得/失 → Jog/Cartesian 面板 CanExecute 联动）
  - `EStopTriggeredMessage`（EStop 触发 → 全局遮罩 + 所有面板强制禁用，无论各面板内部状态如何）
  - `FaultRaisedMessage`（armd 侧故障码 → 日志区高亮 + 顶栏故障徽标）
- `[RelayCommand]` 自动生成 `CanExecute`：例如 `MoveJCommand` 的 `CanExecute` = 已连接 && 已获得控制权 && 已使能 && 未处于 EStop 状态 && 无进行中的运动指令（防止并发下发）。

### 2.3 gRPC 客户端层设计

**Channel 与连接管理（`ArmdClient : IArmdClient`）**
- 使用 `GrpcChannel.ForAddress("http://localhost:50051")`（默认走 WSL2 的 localhost 自动端口转发；端点在设置里可覆盖，为 WSL 转发失效时的手动兜底，比如改用 WSL 网卡 IP）。
- 连接状态通过 `channel.State` + `WaitForStateChangedAsync` 在后台任务里持续观察，而不是轮询，减少无谓 CPU 占用；状态变化经 `IMessenger` 广播给 `ConnectionBarViewModel`。
- 一元 RPC（`AcquireControl` / `ReleaseControl` / `SetEnable` / `EStop` / `MoveJ` / `MoveL` / `SetJogVelocity`）：包一层统一的异常转译（gRPC `RpcException` → 领域内 `ArmdError`，见第 5 节），返回强类型 Result，不把 `RpcException` 泄漏到 ViewModel。

**StreamState 后台任务 → UI 线程 30fps 节流（关键机制，详细说明）**

armd 侧的关节状态/位姿流大概率是高频推送（比如 HardwareLoop 轮询节奏可能到 100Hz+），如果每一帧都直接 `Dispatcher.Invoke` 去更新 UI 绑定属性，会造成 UI 线程消息队列堆积、界面卡顿。设计如下：

1. `JointStreamHostedService`（`IHostedService`）在后台线程用 `await foreach (var state in _armdClient.StreamJointStateAsync(ct))` 消费 server-streaming RPC，**不做任何 UI 相关调用**。
2. 每收到一帧，只做一次 `Volatile.Write` / `Interlocked.Exchange` 把"最新一帧"存入一个共享的 `JointStateSlot`（结构体或不可变快照对象），旧帧直接被覆盖丢弃 —— 这是"latest-wins"策略，天然把高频流"合并"成任意时刻只有一份最新数据，不使用队列，从根上避免背压堆积。
3. UI 侧由一个 `DispatcherTimer`（`Interval = TimeSpan.FromMilliseconds(1000.0/30)`，`DispatcherPriority.Render` 或 `Background`）在 UI 线程按 30fps 节拍读取 `JointStateSlot` 的当前值，只有当值与上次渲染的值不同（或版本号递增）时才更新 6 个 `JointGaugeViewModel` 的绑定属性，避免无变化时的多余属性变更通知。
4. 该机制同样复用于笛卡尔位姿（FK 结果）显示——位姿读数和关节仪表共用同一条流或另开一条流，节流方式一致。
5. 好处：无论 armd 推送频率是 30Hz 还是 500Hz，UI 更新频率恒定在 30fps，观感稳定且可预测；实现上不依赖 Rx（`System.Reactive`）这类额外依赖，`DispatcherTimer` + 原子交换足够。若后续发现节流逻辑复用点变多（比如 v2 视频流也要节流），再考虑抽成通用的 `IThrottledUiPump<T>` 帮助类。

**Jog 的 watchdog 心跳发送**（与第 3.3 节的按住点动交互配合）：Jog 期间由 UI 侧一个独立的 `DispatcherTimer`（周期需明显小于 armd 侧 watchdog 超时阈值，比如超时 200ms 则心跳周期取 50~80ms）持续调用 `SetJogVelocityAsync`；松开按钮/鼠标离开控件/窗口失焦/异常，都必须同步停止该 Timer 并立即下发一次"速度置零"指令，双重保险（心跳停发 + 显式置零）防止意外持续运动。

---

## 3. 页面与控件清单

### 3.1 整体布局

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 顶栏 ConnectionBar: [● 已连接/断线/重连中] [控制权: 已获取/被占用/获取中]     │
│                     [Enable ⏻]           ...           [  E-STOP 大按钮 ]  │
├───────────────┬───────────────────────────────┬───────────────────────────┤
│ 左: 6 关节仪表 │ 中: Jog 面板                    │ 右: 笛卡尔面板              │
│ J1..J6 弧形表  │ 模式: [连续点动] [步进]         │ 当前位姿 X/Y/Z/R/P/Y (只读)│
│ 当前/目标角度  │ 逐关节/笛卡尔轴按住点动按钮      │ 目标位姿输入框              │
│ 限位/故障标色  │ 速度档 slider / 步长档位         │ [MoveJ] [MoveL] 按钮        │
│               │ 参考系: Base / Tool 切换         │ 预置点位下拉(Home 等)      │
├───────────────┴───────────────────────────────┴───────────────────────────┤
│ 底部: 日志区（结构化日志，级别过滤，自动滚动，支持导出）                      │
└───────────────────────────────────────────────────────────────────────────┘
```

v1 单窗口即可承载全部区域（无需 TabControl 分页），机械臂监控/操作类终端更适合"一屏全看见"，避免切页丢上下文（尤其是急停场景不能藏在别的 Tab 后面）。

### 3.2 顶栏 ConnectionBar

| 控件 | 说明 |
|---|---|
| 连接状态 `StatusChip` | 三态：已连接(绿)/连接中(黄, 带脉动动画)/已断开(红)；点击可展开详情(端点地址、最近一次错误) |
| 控制权状态 | 三态：未获取(灰, 附"获取控制"按钮)/已获取(蓝)/被他人占用(橙, 显示占用方标识若 armd 返回)；`AcquireControl`/`ReleaseControl` 按钮 |
| Enable 开关 | ToggleButton，仅在"已获取控制权"时可交互；关闭时所有 Jog/MoveJ/MoveL 命令 `CanExecute=false` |
| 心跳/延迟指示 | 小图标+ms 数值，展示到 armd 的往返延迟，异常升高时变色提示 |
| E-STOP 按钮 | 全终端最高优先级控件：常驻右侧、尺寸最大、红底白字、不受其他区域禁用状态影响（除非已经处于 EStop 触发态，此时按钮变为"复位"语义，见第 5.5 节） |

### 3.3 6 关节仪表（JointMonitorPanel）

- 每个关节一个 `JointGaugeControl`（自定义 UserControl，弧形表盘）：
  - 弧形刻度覆盖该关节的软限位范围（`MinLimit`~`MaxLimit`），当前角度用填充弧/指针表示，目标角度（若正在运动）用细线标出，两者形成"追踪差"视觉。
  - 状态配色（均走 `DynamicResource` 挂 Fluent 语义色，三态主题+高对比自动生效）：正常/运动中/接近限位(黄色告警带)/超限位或故障(红色)/该关节未使能(灰色)。
  - 数值文本显示精确角度（度），故障时额外显示故障码/简述（来自 armd 流里的关节级 fault 字段）。
  - 顶部或旁侧小字标出关节编号/别名（J1…J6，如有语义名如"腰/肩/肘/腕1/腕2/腕3"可配置显示）。
- 6 个仪表用 `UniformGrid` 或 `ItemsControl` 绑定 `ObservableCollection<JointGaugeViewModel>`（长度固定为 6），不做成"通用 N 轴"的过度设计——本项目就是六轴臂，写死 6 更简单可靠。

### 3.4 Jog 面板（JogPanel）

- **模式切换**：`连续点动`（按住=持续运动）/`步进`（点击=单次相对位移），两者互斥单选（ToggleButton 组或 Segmented Control 样式）。
- **连续点动交互**：
  - 每个可点动方向一个 `JogButtonControl`（可以是逐关节 J1+/J1- … J6+/J6- 共 12 个，或笛卡尔轴 X+/X-/Y+/Y-/Z+/Z-/Rx±/Ry±/Rz±，取决于当前选中"关节点动/笛卡尔点动"子模式——两种点动模式都要支持，通过一个额外的子切换控制，复用同一套 JogButtonControl）。
  - 交互事件：`PreviewMouseLeftButtonDown` → 开始点动（启动第 2.3 节所述 watchdog 心跳 Timer，下发非零速度）；`PreviewMouseLeftButtonUp` **和** `MouseLeave` **和** `LostMouseCapture`/窗口 `Deactivated` 都要挂钩 → 立即停止点动（这是安全关键点：用户按住按钮但把鼠标划出控件范围，不能因为没收到 MouseUp 而继续运动，必须用 `MouseLeave`/失去捕获兜底；建议按下时 `CaptureMouse()`，这样 `MouseLeave` 仍能正确收到后续的 `MouseUp`，两个安全网叠加而不是互斥）。
  - 键盘可达性：`JogButtonControl` 需支持获得焦点后用 `Space`/`Enter` 按住触发同样的 Press/Release 语义（`PreviewKeyDown`/`PreviewKeyUp`，注意屏蔽键盘自动重复触发的多次 KeyDown 造成"抖动"，用 `e.IsRepeat` 过滤）——这不是可选项，是第 4.4 节可访问性要求的一部分。
  - 速度档位：Slider 或档位按钮（低速/中速/高速），映射到 `SetJogVelocity` 的速度标量。
- **步进模式交互**：点击 = 立即下发一次相对 `MoveJ`（关节步进）或相对 `MoveL`（笛卡尔步进），步长可配置档位（如 0.5°/1°/5° 或 1mm/5mm/10mm），不需要 watchdog 心跳（本质是一次性有限位移的普通运动指令，armd 侧照常走软限位预检）。
- **参考系切换**（笛卡尔点动时）：Base / Tool，影响 Jog 方向按钮实际下发的坐标系参数。

### 3.5 笛卡尔面板（CartesianPanel）

- 当前位姿只读展示：X/Y/Z（长度单位 mm）+ Roll/Pitch/Yaw 或四元数（跟随 armd FK 输出格式），来自第 2.3 节同一条节流后的状态流。
- 目标位姿输入：6 个数值输入框（或位置+姿态两组），带基本范围校验（客户端侧先做一层"明显越界"拦截，真正的安全限位判断仍以 armd/armd 之下的软限位预检为准，客户端校验只是提前给用户反馈，不替代服务端安全层）。
- `MoveJ` / `MoveL` 按钮：分别对应关节空间插值/直线插值运动，按钮旁可放速度、加速度参数输入（若 v1 API 支持），下发后进入"运动中"状态锁定重复下发（`CanExecute=false` 直到收到运动完成/失败的响应或状态流指示空闲）。
- 预置点位下拉（如"Home"/自定义收藏点）：v1 可先只做 Home 一个硬编码预置点，收藏点管理留作以后迭代点（不在本轮里程碑内展开）。

### 3.6 日志区（LogPanel）

- 结构化日志列表：时间戳、级别（Info/Warning/Error/Fault）、来源（Connection/Control/Jog/Cartesian/Armd-Fault）、消息内容，支持按级别/来源过滤和文本搜索。
- 自动滚动到最新，用户手动上滚时暂停自动滚动（常见日志区体验），有"回到最新"悬浮按钮。
- 支持导出当前日志到文件（故障复盘用）。
- EStop、控制权变化、连接状态变化等关键事件除了走各自 UI 反馈外，也**必须**同时写入日志区，形成可追溯的时间线。

### 3.7 环境引导面板（WSL + usbipd 初始化）

> **为什么必须放在 WPF 侧**：`usbipd` / `wsl.exe` 都是 Windows 侧可执行文件，而项目约定「不在 WSL 里跑 .exe（interop 挂死）」。因此 armd 与 panthera-cli（都跑在 WSL 内）**在架构上不可能自举自己的 USB 通道**。WPF 终端是唯一原生跑在 Windows 上的组件，这项初始化只能由它承担。没有这一步，M-W1 之后的一切（连接 / 监控 / 控制）都无从谈起。

**入口与触发原则**
- 面板从顶栏「未连接」状态展开（或首启向导），**绝不在应用启动时自动执行**。挂载 USB 会把设备从 Windows 侧摘走、并让 WSL 侧独占硬件，属于有副作用的特权操作，必须是显式的用户动作（一个「一键引导」按钮 + 二次确认）。
- 全流程**只做通道建立，绝不下发任何运动指令**，与安全红线一致。

**六个步骤（每步均为「检测 → 执行 → 复检」，幂等，可单独重试）**

| # | 步骤 | 检测 | 执行 | 需管理员 |
|---|---|---|---|---|
| 1 | usbipd 是否安装 | `usbipd --version` | 缺失则给出 `winget install usbipd` 指引，不自动装 | 否 |
| 2 | WSL 发行版是否运行 | `wsl.exe -l --running -q` | `wsl.exe -d <distro> -u <user> -- true`（执行任意命令即拉起发行版） | 否 |
| 3 | 设备是否已 Shared | `usbipd list` 解析目标行状态 | 未共享则 `usbipd bind --busid <id>`（一次性，持久） | **是** |
| 4 | 挂载到 WSL | `usbipd list` 状态是否为 Attached | `usbipd attach --wsl --busid <id>` | 视版本，通常否（已 bind 时） |
| 5 | 串口就绪与权限 | `wsl -- ls /dev/ttyACM*`，要求 **≥4 个**（SDK `check_serial_dev_exist` 的门槛） | 权限不足则提示安装 udev 规则 `KERNEL=="ttyACM*", MODE="0777"`，优先长效方案而非每次 chmod | 否（WSL 内 sudo） |
| 6 | 启动 WSL 后端并联通 | gRPC `daemon status` / camera status 探活 | `wsl -d <distro> -u <user> -- systemctl --user start camerad armd`，随后分别轮询 `:50051` 与 `:50052` 直到机械臂与相机均健康 | 否 |

**关键设计点**
- **按 VID:PID 匹配，不硬编码 busid**：busid 会随插拔的物理 USB 口变化，硬编码必然在换口后失效。以 `VID_CAF1:FFFF` 和本地可选的设备序列号定位目标行，busid 只作为解析结果使用，并在 UI 上显示实际匹配到的 busid 供人工核对；真实序列号不提交到仓库。
- **提权集中一次**：需要管理员的只有步骤 3（可能还有 4）。不要逐步弹 UAC，而是把这些步骤打包成一个提权子进程（`ProcessStartInfo { Verb = "runas" }`）一次性完成；若进程本身已提权则直接执行。UAC 被取消要作为可恢复错误处理，不能让面板卡死。
- **命令全文可见 + 全程写日志区**：每一步执行前把将要运行的命令原样显示出来（特权操作的可审计性），输出与退出码写入日志区，失败时保留 stderr 原文。
- **失败信息要可行动**：区分「设备不存在（机械臂未上电/未插好）」「未 bind（需管理员）」「已被其它 WSL 实例占用」「WSL 未安装/发行版名不对」，而不是笼统报「初始化失败」。
- **配套「解除挂载」**：提供 `usbipd detach --busid <id>`，用于把设备交还 Windows（换机/排障），并在执行前确认 armd 已释放硬件。
- **不与自动重连混淆**：环境引导解决的是「通道尚未建立」，§5.1 的重连状态机解决的是「通道建立过但断了」。两者入口分开，避免重连循环里反复触发特权操作。

### 3.8 SSH 远程部署向导（Pi / WSL 已部署环境）

- 顶栏连接状态左侧提供「SSH 部署」入口；点击后弹出主机、端口、用户名、可选私钥与
  首次主机指纹策略。密码不写入配置，认证只使用 Windows OpenSSH 默认密钥、
  `ssh-agent` 或用户指定私钥。
- 向导只面向**仓库与依赖已经部署完成**的 Linux 主机，不执行 `git clone`、包安装、
  `uv sync` 或源码修改。它通过 `uname`、`/etc/os-release` 与 `/proc/version` 自动识别
  ARM64 Pi、WSL 或普通 Linux；随后在 `$HOME` 的有限深度内识别同时含
  `pyproject.toml`、`armd/`、`deploy/` 的 Panthera-WAM 工作区。
- 启动入口按远端实况选择：优先使用已经安装的 `armd.service` / `camerad.service`
  systemd user service；无 service 时仅允许调用仓库中已存在的 `deploy/panthera-up.zsh`。
  两者都不存在则停止并给出可行动错误，不推测目录、不自动安装。
- 探测和启动成功后，配置保存为 `BackendMode=SshRemote`。WPF 自动重启，保持
  `ssh -N` 双端口转发：Windows `127.0.0.1:50050 → 远端 127.0.0.1:50051`、
  `127.0.0.1:50049 → 远端 127.0.0.1:50052`。因此无需猜测远端网卡地址，也不会要求
  armd/camerad 暴露到公网接口。
- 整个流程只建立连接和启动服务，绝不获取控制权、使能或发送运动指令；成功后的应用
  重启先走正常关闭流程，释放现有 lease 和点动状态，再启动新实例。

---

## 4. 主题系统设计

### 4.1 三态切换与持久化

- `ThemeMode` 三态：`System`（默认，跟随 Windows 明暗设置）/`Light`/`Dark`，在顶栏或设置弹层提供切换入口（下拉或三态分段按钮）。
- 用户选择持久化到 `%LOCALAPPDATA%\Panthera\terminal-settings.json`；启动时**在创建/显示 `MainWindow` 之前**先从设置读出偏好并设置 `Application.Current.ThemeMode`，避免出现"先亮后暗"的启动闪烁。
- 选择 `System` 时依赖 WPF Fluent 对系统主题变化的原生跟随；无需自己监听注册表（这是选定 Fluent 方案在 1.1 节取舍表里已经写明的收益）。
- 代码里访问 `ThemeMode` 需要压制 `WPF0001` 实验性警告，计划阶段记录此事项，避免实现阶段被当作"未处理警告"误报。

### 4.2 自定义控件的主题联动

- `JointGaugeControl` / `JogButtonControl` / `EStopButton` / `StatusChip` 等自定义控件的颜色**一律**通过 `DynamicResource` 引用 Fluent 语义色资源键（如 `AccentFillColorDefaultBrush`、`SystemFillColorCriticalBrush` 等同类语义键，具体键名在实现阶段对照 Fluent 资源字典确认），**不写死 RGB 硬编码色值**——这样三态切换和高对比模式对自定义控件同样自动生效，不需要额外维护三套配色。
- 例外：EStop 按钮的"危险红"允许在遵循最低对比度要求的前提下使用固定强调色（不完全依赖 Accent 色），因为 EStop 的视觉识别一致性（"永远是那个红色"）比跟随主题更重要；但仍需在 Light/Dark/HC 三种背景下分别验证对比度达标。

### 4.3 高对比与可访问性

- 依赖 Fluent 主题在系统开启"高对比"时自动切换到内建 `HC.xaml` 变体（第 1.1 节已核实），验证重点放在**自定义控件**是否正确响应（因为它们不是 Fluent 内建控件，需要人工确认 `DynamicResource` 绑定在 HC 下取到了合理的对比色，而不是想当然）。
- 关键状态不能只靠颜色区分（色盲友好）：例如关节"故障"态除了变红，还需叠加图标（如警示三角）或文本标签；连接状态 Chip 除了颜色还有文字（"已连接/连接中/已断开"）。
- 字号：跟随 Windows 系统文本缩放（不在应用内单独做一套字号缩放设置，减少和系统设置打架的可能）。
- 完整键盘可达性：所有关键操作（获取控制权、Enable、EStop、Jog 按住、MoveJ/MoveL 触发）都必须能不靠鼠标完成，`Tab` 顺序清晰，焦点视觉明显（Fluent 默认焦点框基本够用，重点检查自定义控件是否正确继承了焦点视觉样式）。

---

## 5. 与 armd 的错误处理

### 5.1 连接生命周期状态机

```
Disconnected ──connect──▶ Connecting ──成功──▶ Connected(未获得控制权)
     ▲                        │失败                    │AcquireControl
     │                        ▼                        ▼
     └──────channel 变 TransientFailure/Shutdown──── Connected(已获得控制权, 可能已 Enable, 可能在 Streaming)
```

- 通过 `channel.State` + `WaitForStateChangedAsync` 驱动状态机迁移，而非轮询；每次迁移经 `IMessenger` 广播 `ConnectionStateChangedMessage`，各面板据此联动置灰/解禁。
- 重连策略：断线后自动重连，退避序列（如 1s/2s/5s/10s，封顶）+ 顶栏提供手动"立即重连"按钮（用户不想干等退避窗口时可以主动触发）。
- **重连后不能假装什么都没发生**：控制权、Enable 状态、Jog 心跳等都不会跨连接持久化，重连成功后必须重新走一遍 `AcquireControl`（如果之前持有）→ 重新订阅状态流 → 等到拿到至少一帧完整状态之后，才把 Jog/MoveJ/MoveL 按钮重新解禁，避免"界面看着能点，其实状态是重连前的旧值"这种危险的信息滞后。

### 5.2 控制权被抢占 / 获取失败

- `AcquireControl` 失败（对应 gRPC `PermissionDenied`/`FailedPrecondition` 或业务返回码）时，顶栏"控制权"状态切到"被占用"，如果 armd 的响应里带了占用方标识（比如是 `panthera-cli` 还是另一个 WPF 实例），展示出来帮助用户判断要不要去找人协调；v1 不做"强制抢占"功能（架构上互斥语义就是为了安全，抢占是个需要谨慎设计的功能，留到有明确需求再做）。
- 控制权在使用中途被 armd 侧收回（比如另一端强制操作，如果协议支持的话）：视为与断线同级别的紧急事件，走 `ControlOwnershipChangedMessage` 立即让所有运动类命令 `CanExecute=false`，并在日志区/顶栏明确提示"控制权已丢失"，而不是静默失效。

### 5.3 心跳/Watchdog 与客户端侧异常

- Jog 心跳发送循环若连续失败 N 次（网络抖动、armd 短暂无响应），客户端本地立即停止点动状态（不等 armd 的 watchdog 超时来救场，客户端是双保险中更快的一层），并把 UI 从"点动中"切回"空闲"，提示用户重试。
- 心跳发送本身若抛异常（`RpcException`），要能被最外层 try/catch 兜住，绝不能让后台心跳 Timer 里的未处理异常导致进程崩溃或 Timer 静默停摆而 UI 却还显示"点动中"。

### 5.4 错误分类与友达呈现

统一在 `Grpc` 层做一次"翻译"，把底层 `RpcException.StatusCode` 映射为面向用户的分类，ViewModel 只消费分类后的领域错误，不感知 gRPC 细节：

| gRPC StatusCode | 领域分类 | 呈现方式 |
|---|---|---|
| `Unavailable` / `DeadlineExceeded` | 网络/连接类 | 顶栏状态变红 + 触发重连流程，日志区记录，不用弹窗打断操作 |
| `PermissionDenied` / `FailedPrecondition`（控制权相关） | 权限类 | 顶栏"控制权"状态提示，相关面板置灰，附一条可展开的说明 |
| 业务码：软限位/EStop/安全联锁 | 安全联锁类 | 高优先级横幅/遮罩（尤其 EStop，见 5.5），必须显式提示，不能只安静记日志 |
| armd 上报的硬件故障码（关节级/整机级） | 硬件故障类 | 对应关节仪表标红+故障简述，日志区记录完整故障码，提供"复制诊断信息"按钮方便反馈给硬件侧 |
| 其它未分类异常 | 未知错误 | 兜底展示原始信息 + 建议用户截图反馈，同时完整记日志，不能吞掉 |

### 5.5 EStop 状态呈现

- EStop 是硬件直通路径（架构既定），UI 侧对应：
  - 状态流里一旦出现 EStop 已触发标志，**无视其他所有局部禁用状态**，立即用全屏/顶层遮罩（半透明红色横幅，居中大字"急停已触发"）覆盖 Jog/Cartesian 操作区，杜绝用户在慌乱中误触已经不该响应的控件。
  - 顶栏 EStop 按钮此时切换视觉与语义为"复位"（具体复位流程要看 armd 对应的清障/复位 RPC 是什么语义，客户端只是如实呈现服务端状态，不在客户端臆造一套复位逻辑）。
  - EStop 触发/解除都是最高优先级日志事件，必须带精确时间戳，方便事后追溯"什么时候触发的、间隔多久解除的"。

---

## 6. 里程碑

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| **M0 脚手架** | 解决方案分层建好（2.1 节结构）；DI/Host 接入；Fluent `ThemeMode` 三态 POC（先只做一个空窗口验证 System/Light/Dark 切换与持久化）；`IArmdClient` 接口 + 一个可先行联调的最小 gRPC 连接（哪怕先只有 health-check 级别的一元调用） | 能启动窗口、能三态换肤、能看到与 armd 的 gRPC 连接状态变化 |
| **M0.5 环境引导** | 3.7 节六步引导面板：usbipd 检测/bind/attach（按 VID:PID 匹配，不硬编码 busid）、WSL 拉起、`/dev/ttyACM*` 就绪校验、armd 启动与探活；一次性提权、命令可见、全程写日志 | 冷启动（WSL 未起、USB 未挂）状态下，点一次「一键引导」即可到达 armd 健康可连；拔插换 USB 口后仍能正确匹配；取消 UAC 能优雅回退不卡死 |
| **M1 关节监控** | `StreamJointStateAsync` 打通；6 个 `JointGaugeControl` 接入 30fps 节流管线；顶栏连接状态/控制权/心跳延迟完整 | 6 轴角度实时刷新稳定不卡顿；断开 armd 能看到状态正确切换并自动重连 |
| **M2 Jog 点动** | 连续点动（按住+watchdog 心跳+安全兜底事件）与步进点动全部跑通；速度/步长档位可调；键盘可达性到位 | 按住/松开/鼠标划出/窗口失焦均能正确启停运动；心跳异常能触发客户端侧立即停止 |
| **M3 笛卡尔面板** | FK 位姿只读展示接入节流管线；目标位姿输入 + `MoveJ`/`MoveL` 下发；Home 预置点位 | 能下发一次 MoveJ/MoveL 并看到位姿与关节仪表同步变化，运动中重复下发被正确拦截 |
| **M4 错误处理与打磨** | 控制权抢占/丢失场景、EStop 横幅、错误分类呈现（5.4 节表格）全部实现；日志区完整（含导出）；高对比/键盘可达性走一遍可用性检查 | 断连-重连、控制权冲突、EStop 触发-解除三类关键场景手动演练均表现符合第 5 节设计；HC 模式下自定义控件可辨识 |
| M5（v2 预告，超出本轮范围，仅记录以保持架构一致性） | 拖动示教录制回放 UI、D405 视频流面板、LeRobot 数据采集控制面板 | 不在本计划详细展开，v2 立项时另开设计文档 |

---

## 7. 假设与开放风险（供实现阶段确认）

1. **`ThemeMode` 实验性状态**：目前标记 `WPF0001`，若 .NET 9 后续 Servicing 或 .NET 10 有 API 变化，需要跟踪并调整；这是选型阶段已知且可接受的风险，不是遗漏。
2. **远程链路边界**：主路径依赖 Windows 到 Pi IP 的稳定连通；当前 gRPC 为明文 HTTP/2，
   应使用 Tailscale 或受信 LAN，并限制 50051/50052 的来源。WSL localhost bridge 仅是兼容回退。
3. **armd 是否提供"控制权占用方标识"字段**：5.2 节的"显示占用方"依赖 armd 协议是否暴露该信息，若协议里没有，UI 只能展示"被占用"而无法说明是谁，需要和 armd/协议设计侧确认 `.proto` 字段。
4. **强制抢占控制权**：v1 明确不做，但架构/协议设计时建议预留（哪怕先不在 UI 暴露），避免真出现"占用方已经不在了但没正常 Release"的死锁场景时无解。
5. **Fluent 语义色资源键的具体命名**：本文档引用的资源键名（如 `AccentFillColorDefaultBrush`、`SystemFillColorCriticalBrush` 等）基于 Fluent 主题的通用色板设计模式，实现阶段需对照 .NET 9 SDK 实际内建的 Fluent ResourceDictionary 逐一核实键名，不能凭本文档直接照抄。
6. **usbipd attach 的提权要求随版本变化**（3.7 节步骤 4）：较新的 usbipd-win 在设备已 `bind`（Shared）后允许非管理员 `attach`，旧版本则要求管理员。实现阶段需按目标机实际版本确认，UI 上不要把「需要管理员」写死，而应在非提权尝试失败时再回退到提权路径。
7. **发行版名与用户名不可硬编码**（3.7 节步骤 2/6）：`wsl -d <distro> -u <user>` 的取值应来自设置文件（默认可预填当前环境值），否则换机即失效；引导面板需能列出 `wsl -l -q` 的候选供选择。
8. **后端启动方式**：Pi 主路径与 WSL 回退都优先使用 systemd user service 管理
   `camerad:50052` 与 `armd:50051`。`Remote` 模式仍只负责直连探活；用户显式选择
   `SshRemote` 向导时，WPF 才通过 OpenSSH 探测并启动既有服务/脚本，并维护本地端口转发。
