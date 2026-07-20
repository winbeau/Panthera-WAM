using System.Collections.ObjectModel;
using System.Threading.Channels;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Panthera.Terminal.Core;

public sealed partial class MainWindowViewModel : ObservableObject
{
    private static readonly double[] JointMinimum = [-2.4, -0.1, -0.1, -1.6, -1.7, -2.5];
    private static readonly double[] JointMaximum = [2.4, 3.2, 4.0, 1.6, 1.7, 2.5];
    private static readonly double[] HomeJointPosition = [0.0, 0.6, 0.6, 0.0, 0.0, 0.0];
    private static readonly DemoStep[] DemoSequence =
    [
        new("安全复位位", HomeJointPosition, 2.5),
        new("右侧展示位", [0.5, 0.8, 0.8, 0.3, 0.0, 0.0], 3.0),
        new("左侧展示位", [-0.3, 1.2, 1.2, 0.4, 0.0, 0.0], 3.0),
        new("返回安全位", HomeJointPosition, 2.5),
    ];
    private readonly IArmdClient _client;
    private readonly IEnvironmentGuideService _environmentGuide;
    private readonly ITerminalSettingsStore _settingsStore;
    private readonly SemaphoreSlim _jogGate = new(1, 1);
    private CancellationTokenSource? _jogPumpLifetime;
    private CancellationTokenSource? _jogCallLifetime;
    private CancellationTokenSource? _demoLifetime;
    private Channel<IReadOnlyList<double>>? _jogChannel;
    private Task? _jogPumpTask;
    private Task? _jogCallTask;
    private string _activeExecutionId = string.Empty;
    private string _activeDatasetJobId = string.Empty;
    private DateTimeOffset? _recordingStartedAt;
    private int _poseRefreshActive;
    private bool _jointTargetsInitialized;
    private bool _cartesianTargetInitialized;
    private double _gripperMinimum;
    private double _gripperMaximum = 1.6;

    public MainWindowViewModel(
        IArmdClient client,
        IEnvironmentGuideService environmentGuide,
        ITerminalSettingsStore settingsStore,
        TerminalSettings settings)
    {
        _client = client;
        _environmentGuide = environmentGuide;
        _settingsStore = settingsStore;
        Settings = settings;
        Joints = new ObservableCollection<JointGaugeViewModel>(
            Enumerable.Range(0, 6).Select(index => new JointGaugeViewModel(
                index,
                $"J{index + 1}",
                JointMinimum[index],
                JointMaximum[index])));
        Theme = settings.Theme;
        UiScale = Math.Clamp(settings.UiScale, 0.90, 1.40);
        JogSpeed = settings.JogSpeed;
        DatasetRepoId = "local/panthera-wam";
        DatasetTask = "Panthera demonstration";
        TeachRecordingName = NextRecordingName();
        ConnectionDetail = $"arm {settings.Endpoint} · camera {settings.CameraEndpoint}";
    }

    public TerminalSettings Settings { get; private set; }

    public ObservableCollection<JointGaugeViewModel> Joints { get; }

    public ObservableCollection<TerminalLogEntry> Logs { get; } = [];

    public ObservableCollection<EnvironmentGuideStep> EnvironmentSteps { get; } = [];

    public ObservableCollection<TeachRecordingSnapshot> TeachRecordings { get; } = [];

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(AcquireControlCommand))]
    private TerminalConnectionState _connectionState = TerminalConnectionState.Disconnected;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ReleaseControlCommand))]
    [NotifyCanExecuteChangedFor(nameof(MoveJCommand))]
    [NotifyCanExecuteChangedFor(nameof(MoveLCommand))]
    [NotifyCanExecuteChangedFor(nameof(GripperOpenCommand))]
    [NotifyCanExecuteChangedFor(nameof(GripperCloseCommand))]
    [NotifyCanExecuteChangedFor(nameof(ResetArmCommand))]
    [NotifyCanExecuteChangedFor(nameof(ToggleDemoSequenceCommand))]
    private bool _hasControl;

    [ObservableProperty]
    private string _controlHolder = "无";

    [ObservableProperty]
    private bool _eStopEngaged;

    [ObservableProperty]
    private string _daemonSummary = "等待连接";

    [ObservableProperty]
    private bool _cameraAvailable;

    [ObservableProperty]
    private string _cameraSummary = "等待 camerad";

    [ObservableProperty]
    private string _connectionDetail = string.Empty;

    [ObservableProperty]
    private long _stateAgeMs;

    [ObservableProperty]
    private double _gripperPosition;

    [ObservableProperty]
    private double _gripperVelocity;

    [ObservableProperty]
    private double _gripperTorque;

    [ObservableProperty]
    private bool _gripperValid;

    [ObservableProperty]
    private uint _gripperFault;

    [ObservableProperty]
    private double _tcpX;

    [ObservableProperty]
    private double _tcpY;

    [ObservableProperty]
    private double _tcpZ;

    [ObservableProperty]
    private double _targetX;

    [ObservableProperty]
    private double _targetY;

    [ObservableProperty]
    private double _targetZ;

    [ObservableProperty]
    private double _targetRoll;

    [ObservableProperty]
    private double _targetPitch;

    [ObservableProperty]
    private double _targetYaw;

    [ObservableProperty]
    private bool _preserveOrientation = true;

    [ObservableProperty]
    private double _targetJ1;

    [ObservableProperty]
    private double _targetJ2;

    [ObservableProperty]
    private double _targetJ3;

    [ObservableProperty]
    private double _targetJ4;

    [ObservableProperty]
    private double _targetJ5;

    [ObservableProperty]
    private double _targetJ6;

    [ObservableProperty]
    private double _targetMoveJDuration = 3.0;

    [ObservableProperty]
    private double _targetMoveLDuration = 3.0;

    [ObservableProperty]
    private double _jogSpeed;

    [ObservableProperty]
    private double _executionFraction;

    [ObservableProperty]
    private string _executionStatus = "空闲";

    [ObservableProperty]
    private bool _isBusy;

    [ObservableProperty]
    private bool _isJogging;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ToggleDemoSequenceCommand))]
    private bool _isDemoRunning;

    [ObservableProperty]
    private bool _isEnvironmentBusy;

    [ObservableProperty]
    private string _theme;

    [ObservableProperty]
    private double _uiScale;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(PlayTeachRecordingCommand))]
    [NotifyCanExecuteChangedFor(nameof(ExportDatasetCommand))]
    private TeachRecordingSnapshot? _selectedTeachRecording;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(StartTeachCommand))]
    [NotifyCanExecuteChangedFor(nameof(StopTeachCommand))]
    [NotifyCanExecuteChangedFor(nameof(StartTeachRecordingCommand))]
    private bool _isTeachActive;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(StartTeachRecordingCommand))]
    [NotifyCanExecuteChangedFor(nameof(StopTeachRecordingCommand))]
    private bool _isTeachRecording;

    [ObservableProperty]
    private string _teachStatus = "未启动";

    [ObservableProperty]
    private string _recordingPath = string.Empty;

    [ObservableProperty]
    private string _teachRecordingName = string.Empty;

    [ObservableProperty]
    private double _teachRecordingElapsedSeconds;

    [ObservableProperty]
    private long _teachRecordingFrameCount;

    [ObservableProperty]
    private string _datasetRepoId = string.Empty;

    [ObservableProperty]
    private string _datasetTask = string.Empty;

    [ObservableProperty]
    private string _datasetOutputDirectory = string.Empty;

    [ObservableProperty]
    private double _datasetProgress;

    [ObservableProperty]
    private string _datasetStatus = "IDLE";

    [ObservableProperty]
    private string _datasetResult = string.Empty;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ExportDatasetCommand))]
    [NotifyCanExecuteChangedFor(nameof(CancelDatasetExportCommand))]
    private bool _isDatasetBusy;

    public bool CanControl => HasControl
        && _client.HasActiveLease
        && !EStopEngaged
        && ConnectionState == TerminalConnectionState.Connected
        && !IsBusy;

    public bool IsConnected => ConnectionState == TerminalConnectionState.Connected;

    public string ConnectionLabel => ConnectionState switch
    {
        TerminalConnectionState.Connected => "已连接",
        TerminalConnectionState.Connecting => "连接中",
        TerminalConnectionState.Faulted => "连接异常",
        _ => "未连接",
    };

    public string ControlSummary => HasControl
        ? $"控制权 · {ControlHolder}"
        : ControlHolder == "无" ? "未持有控制权" : $"控制权 · {ControlHolder}";

    public string LiveSummary => $"LIVE · {StateAgeMs} ms";

    public string UiScaleLabel => $"{UiScale * 100:F0}%";

    public string DemoActionLabel => IsDemoRunning ? "停止展示" : "展示动作";

    public double GripperPercent
    {
        get
        {
            var range = _gripperMaximum - _gripperMinimum;
            return range <= 0 ? 0 : Math.Clamp((GripperPosition - _gripperMinimum) / range * 100.0, 0, 100);
        }
    }

    public string GripperStatus => !GripperValid
        ? "离线"
        : GripperFault != 0 ? $"故障 0x{GripperFault:X2}" : "就绪";

    public string GripperOpeningState => !GripperValid
        ? "OFFLINE"
        : GripperPercent <= 8 ? "CLOSED" : GripperPercent >= 92 ? "OPEN" : "PARTIAL";

    public string TeachRecordingElapsedLabel
    {
        get
        {
            var elapsed = TimeSpan.FromSeconds(Math.Max(0, TeachRecordingElapsedSeconds));
            return $"{(int)elapsed.TotalHours:00}:{elapsed.Minutes:00}:{elapsed.Seconds:00}";
        }
    }

    public string TeachRecordingSummary => IsTeachRecording
        ? $"REC · {TeachRecordingElapsedLabel}"
        : IsTeachActive
            ? "拖动示教已开启 · 等待录制"
            : TeachRecordingFrameCount > 0
                ? $"已保存 {TeachRecordingFrameCount:N0} frames"
                : "准备就绪";

    public async Task InitializeAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            var daemon = await _client.GetDaemonStatusAsync(cancellationToken);
            var camera = await _client.GetCameraStatusAsync(cancellationToken);
            var control = await _client.GetControlStatusAsync(cancellationToken);
            var limits = await _client.GetSoftLimitsAsync(cancellationToken);
            ConnectionState = _client.ConnectionState;
            HasControl = control.Held
                && control.HolderClientId == Environment.MachineName
                && _client.HasActiveLease;
            ControlHolder = control.Held ? control.HolderClientId : "无";
            EStopEngaged = control.EStopEngaged;
            DaemonSummary = daemon.Simulation
                ? $"仿真 · {daemon.ControlHz:F0} Hz"
                : $"真机 {(daemon.HardwareConnected ? "在线" : "未连接")} · {daemon.ControlHz:F0} Hz";
            CameraAvailable = camera.Available && camera.Streaming;
            CameraSummary = CameraAvailable
                ? $"camerad 采集 · {camera.ActualFps:F1} fps"
                : camera.Enabled ? camera.Error : "camerad 未启用相机";
            ConnectionDetail = $"arm {Settings.Endpoint} · camera {Settings.CameraEndpoint}";
            _gripperMinimum = limits.GripperMinimum;
            _gripperMaximum = limits.GripperMaximum;
            OnPropertyChanged(nameof(GripperPercent));
            for (var index = 0; index < Math.Min(Joints.Count, limits.Joints.Count); index++)
            {
                Joints[index].Minimum = limits.Joints[index].Minimum;
                Joints[index].Maximum = limits.Joints[index].Maximum;
            }
            AddLog("Info", "Connection", $"机械臂 {Settings.Endpoint}");
            AddLog("Info", "Connection", $"相机 {Settings.CameraEndpoint}");
            AddLog(CameraAvailable ? "Info" : "Warning", "D405", CameraSummary);
            await RefreshTeachRecordingsAsync();
        }
        catch (Exception exception)
        {
            ConnectionState = _client.ConnectionState;
            ConnectionDetail = exception.Message;
            AddLog("Error", "Connection", exception.Message);
        }
    }

    public void ApplySnapshot(RobotSnapshot snapshot)
    {
        ReconcileControlLease();
        StateAgeMs = snapshot.AgeMs;
        EStopEngaged = snapshot.EStopEngaged;
        ConnectionState = TerminalConnectionState.Connected;
        for (var index = 0; index < Math.Min(6, snapshot.Joints.Count); index++)
        {
            var source = snapshot.Joints[index];
            var target = Joints[index];
            target.Position = source.Position;
            target.Velocity = source.Velocity;
            target.Torque = source.Torque;
            target.Fault = source.Fault;
            target.Valid = source.Valid;
            target.LimitWarning = source.PositionLimitFlag != 0 || source.TorqueLimitFlag != 0
                || source.Position < target.Minimum + 0.08
                || source.Position > target.Maximum - 0.08;
        }
        GripperPosition = snapshot.Gripper.Position;
        GripperVelocity = snapshot.Gripper.Velocity;
        GripperTorque = snapshot.Gripper.Torque;
        GripperFault = snapshot.Gripper.Fault;
        GripperValid = snapshot.Gripper.Valid;
        if (IsTeachRecording && _recordingStartedAt is not null)
        {
            TeachRecordingElapsedSeconds = (DateTimeOffset.UtcNow - _recordingStartedAt.Value).TotalSeconds;
        }
        if (!_jointTargetsInitialized && snapshot.Joints.Count >= 6)
        {
            TargetJ1 = snapshot.Joints[0].Position;
            TargetJ2 = snapshot.Joints[1].Position;
            TargetJ3 = snapshot.Joints[2].Position;
            TargetJ4 = snapshot.Joints[3].Position;
            TargetJ5 = snapshot.Joints[4].Position;
            TargetJ6 = snapshot.Joints[5].Position;
            _jointTargetsInitialized = true;
        }
        _ = RefreshPoseAsync(snapshot.Joints.Select(joint => joint.Position).ToArray());
    }

    public async Task ShutdownAsync()
    {
        RequestDemoStop();
        await StopJogAsync();
        if (IsTeachRecording)
        {
            await StopTeachRecordingAsync();
        }
        if (IsTeachActive)
        {
            await StopTeachAsync();
        }
        if (IsDatasetBusy && !string.IsNullOrWhiteSpace(_activeDatasetJobId))
        {
            await _client.CancelDatasetJobAsync(_activeDatasetJobId);
        }
        if (!string.IsNullOrEmpty(_activeExecutionId))
        {
            await CancelExecutionAsync();
        }
        if (HasControl)
        {
            try
            {
                await _client.ReleaseControlAsync();
            }
            catch (Exception exception)
            {
                AddLog("Warning", "Control", $"释放控制权失败：{exception.Message}");
            }
        }
    }

    [RelayCommand(CanExecute = nameof(CanAcquireControl))]
    private async Task AcquireControlAsync()
    {
        try
        {
            var status = await _client.AcquireControlAsync(Environment.MachineName);
            HasControl = status.Held && _client.HasActiveLease;
            ControlHolder = status.HolderClientId;
            AddLog(status.Held ? "Info" : "Warning", "Control",
                status.Held ? "已获取控制权" : $"控制权由 {status.HolderClientId} 持有");
        }
        catch (Exception exception)
        {
            AddLog("Error", "Control", exception.Message);
        }
    }

    private bool CanAcquireControl() => ConnectionState == TerminalConnectionState.Connected && !HasControl;

    [RelayCommand(CanExecute = nameof(CanReleaseControl))]
    private async Task ReleaseControlAsync()
    {
        try
        {
            RequestDemoStop();
            await StopJogAsync();
            await _client.ReleaseControlAsync();
            AddLog("Info", "Control", "已释放控制权");
        }
        catch (Exception exception)
        {
            AddLog("Warning", "Control", $"释放控制权失败，等待后端 watchdog 收尾：{exception.Message}");
        }
        finally
        {
            HasControl = false;
            ControlHolder = "无";
        }
    }

    private bool CanReleaseControl() => HasControl;

    [RelayCommand]
    private async Task TriggerEStopAsync()
    {
        try
        {
            RequestDemoStop();
            await StopJogAsync();
            await _client.TriggerEStopAsync("WPF operator E-STOP");
            EStopEngaged = true;
            AddLog("Fault", "Safety", "已触发急停");
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Safety", exception);
        }
    }

    [RelayCommand]
    private async Task ClearEStopAsync()
    {
        if (!HasControl)
        {
            AddLog("Warning", "Safety", "复位急停前需获取控制权");
            return;
        }
        try
        {
            await StopJogAsync();
            await _client.ClearEStopAsync();
            EStopEngaged = false;
            AddLog("Info", "Safety", "急停已复位，高阻尼安全恢复已建立");
        }
        catch (Exception exception)
        {
            EStopEngaged = true;
            AddLog("Error", "Safety", $"急停复位失败，保持急停：{exception.Message}");
        }
    }

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private async Task MoveJAsync()
    {
        await RunBusyAsync("MoveJ", async () =>
        {
            var result = await _client.MoveJAsync(
                [TargetJ1, TargetJ2, TargetJ3, TargetJ4, TargetJ5, TargetJ6],
                TargetMoveJDuration,
                true);
            AddLog(result.Accepted && result.Reached ? "Info" : "Warning", "Motion",
                result.Accepted ? $"MoveJ 完成，到位={result.Reached}" : result.RejectReason);
        });
    }

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private async Task ResetArmAsync()
    {
        await RunBusyAsync("Home", async () =>
        {
            SetJointTargets(HomeJointPosition);
            var result = await _client.MoveJAsync(HomeJointPosition, 3.0, true);
            AddLog(result.Accepted && result.Reached ? "Info" : "Warning", "Motion",
                result.Accepted && result.Reached
                    ? "已复位到安全姿态 J=[0.0, 0.6, 0.6, 0.0, 0.0, 0.0]"
                    : string.IsNullOrWhiteSpace(result.RejectReason)
                        ? "复位未到位"
                        : result.RejectReason);
        });
    }

    [RelayCommand(AllowConcurrentExecutions = true, CanExecute = nameof(CanToggleDemoSequence))]
    private async Task ToggleDemoSequenceAsync()
    {
        if (IsDemoRunning)
        {
            RequestDemoStop(logRequest: true);
            return;
        }
        if (!CanRunMotion())
        {
            return;
        }

        _demoLifetime = new CancellationTokenSource();
        var cancellationToken = _demoLifetime.Token;
        IsDemoRunning = true;
        IsBusy = true;
        NotifyMotionCommands();
        AddLog("Info", "Demo", "展示动作已启动；再次点击将在当前 MoveJ 段完成后停止");
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                foreach (var step in DemoSequence)
                {
                    if (cancellationToken.IsCancellationRequested)
                    {
                        break;
                    }
                    SetJointTargets(step.Positions);
                    ExecutionStatus = $"展示 · {step.Name}";
                    var result = await _client.MoveJAsync(step.Positions, step.DurationSeconds, true);
                    if (cancellationToken.IsCancellationRequested)
                    {
                        break;
                    }
                    if (!result.Accepted || !result.Reached)
                    {
                        throw new InvalidOperationException(
                            string.IsNullOrWhiteSpace(result.RejectReason)
                                ? $"{step.Name} 未到位"
                                : result.RejectReason);
                    }
                    await Task.Delay(TimeSpan.FromMilliseconds(350), cancellationToken);
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Demo", exception);
        }
        finally
        {
            _demoLifetime?.Dispose();
            _demoLifetime = null;
            IsDemoRunning = false;
            IsBusy = false;
            ExecutionStatus = "空闲";
            NotifyMotionCommands();
            AddLog("Info", "Demo", "展示动作已停止");
        }
    }

    private bool CanToggleDemoSequence() => IsDemoRunning || CanRunMotion();

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private async Task MoveLAsync()
    {
        await RunBusyAsync("MoveL", async () =>
        {
            var handle = await _client.MoveLAsync(
                new CartesianTarget(
                    TargetX,
                    TargetY,
                    TargetZ,
                    TargetRoll,
                    TargetPitch,
                    TargetYaw,
                    PreserveOrientation),
                TargetMoveLDuration);
            _activeExecutionId = handle.ExecutionId;
            ExecutionStatus = "运行中";
            await foreach (var progress in _client.StreamExecutionAsync(handle.ExecutionId))
            {
                ExecutionFraction = progress.Fraction;
                ExecutionStatus = progress.State switch
                {
                    ExecutionState.Done => "完成",
                    ExecutionState.Cancelled => "已取消",
                    ExecutionState.Failed => $"失败：{progress.ErrorMessage}",
                    _ => "运行中",
                };
            }
            AddLog(ExecutionStatus == "完成" ? "Info" : "Warning", "Motion", $"MoveL {ExecutionStatus}");
            _activeExecutionId = string.Empty;
        });
    }

    [RelayCommand]
    private async Task CancelExecutionAsync()
    {
        if (string.IsNullOrEmpty(_activeExecutionId))
        {
            return;
        }
        try
        {
            var cancelled = await _client.CancelExecutionAsync(_activeExecutionId);
            AddLog(cancelled ? "Info" : "Warning", "Motion", cancelled ? "已请求平滑取消" : "执行已结束或不存在");
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Motion", exception);
        }
    }

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private Task GripperOpenAsync() => RunGripperAsync(1.6, "打开");

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private Task GripperCloseAsync() => RunGripperAsync(0.0, "闭合");

    private async Task RunGripperAsync(double position, string label)
    {
        await RunBusyAsync("Gripper", async () =>
        {
            var result = await _client.GripperMoveAsync(position, 0.35);
            AddLog(result.Accepted ? "Info" : "Warning", "Gripper",
                result.Accepted ? $"夹爪{label}指令已接受" : result.RejectReason);
        });
    }

    private bool CanRunMotion() => CanControl && !IsTeachActive && !IsTeachRecording;

    [RelayCommand]
    private async Task StartJogAsync(string? parameter)
    {
        if (!CanRunMotion() || !TryParseJog(parameter, out var jointIndex, out var direction))
        {
            return;
        }
        await _jogGate.WaitAsync();
        try
        {
            await StopJogCoreAsync();
            _jogChannel = Channel.CreateBounded<IReadOnlyList<double>>(new BoundedChannelOptions(2)
            {
                FullMode = BoundedChannelFullMode.DropOldest,
                SingleReader = true,
                SingleWriter = true,
            });
            _jogCallLifetime = new CancellationTokenSource();
            _jogPumpLifetime = new CancellationTokenSource();
            _jogCallTask = _client.JogAsync(
                _jogChannel.Reader.ReadAllAsync(_jogCallLifetime.Token),
                _jogCallLifetime.Token);
            _jogPumpTask = PumpJogAsync(jointIndex, direction, _jogPumpLifetime.Token);
            _ = MonitorJogAsync(_jogCallTask);
            IsJogging = true;
        }
        catch (Exception exception)
        {
            await StopJogCoreAsync();
            ConnectionState = _client.ConnectionState;
            HandleOperationFailure("Jog", new InvalidOperationException($"点动启动失败：{exception.Message}", exception));
        }
        finally
        {
            _jogGate.Release();
        }
    }

    [RelayCommand]
    public async Task StopJogAsync()
    {
        await _jogGate.WaitAsync();
        try
        {
            await StopJogCoreAsync();
        }
        finally
        {
            _jogGate.Release();
        }
    }

    [RelayCommand]
    private async Task ProbeEnvironmentAsync() => await RunEnvironmentAsync(run: false);

    [RelayCommand]
    private async Task RunEnvironmentGuideAsync() => await RunEnvironmentAsync(run: true);

    public void SaveTheme(string theme)
    {
        Theme = theme;
        Settings = Settings with { Theme = theme, JogSpeed = JogSpeed };
        _settingsStore.Save(Settings);
    }

    [RelayCommand]
    private void ZoomIn() => SetUiScale(UiScale + 0.05);

    [RelayCommand]
    private void ZoomOut() => SetUiScale(UiScale - 0.05);

    [RelayCommand]
    private void ResetZoom() => SetUiScale(1.0);

    [RelayCommand(CanExecute = nameof(CanStartTeachSession))]
    private async Task StartTeachSessionAsync()
    {
        await RunBusyAsync("Teach session", async () =>
        {
            if (!HasControl || !_client.HasActiveLease)
            {
                ReconcileControlLease();
                var control = await _client.AcquireControlAsync(Environment.MachineName);
                HasControl = control.Held && _client.HasActiveLease;
                ControlHolder = control.HolderClientId;
                if (!HasControl)
                {
                    TeachStatus = $"无法获取控制权：{control.HolderClientId}";
                    AddLog("Warning", "Teach", TeachStatus);
                    return;
                }
            }

            var teach = await _client.StartTeachAsync();
            if (!teach.Accepted)
            {
                TeachStatus = $"示教启动被拒绝：{teach.RejectReason}";
                AddLog("Warning", "Teach", TeachStatus);
                return;
            }
            IsTeachActive = true;
            try
            {
                var requestedPath = RecordingRequestPath(TeachRecordingName);
                var path = await _client.StartTeachRecordingAsync(requestedPath);
                BeginTeachRecording(path);
                TeachStatus = "拖动示教与轨迹录制进行中";
                AddLog("Info", "Recorder", $"示教录制已开始：{path}");
            }
            catch
            {
                try
                {
                    await _client.StopTeachAsync();
                }
                finally
                {
                    IsTeachActive = false;
                    TeachStatus = "录制启动失败，已退出拖动示教";
                }
                throw;
            }
        });
    }

    private bool CanStartTeachSession() =>
        IsConnected && !EStopEngaged && !IsTeachActive && !IsTeachRecording && !IsBusy;

    [RelayCommand(CanExecute = nameof(CanStopTeachSession))]
    private async Task StopTeachSessionAsync()
    {
        await RunBusyAsync("Teach session", async () =>
        {
            TeachRecordingSnapshot? recording = null;
            var wasRecording = IsTeachRecording;
            if (wasRecording)
            {
                try
                {
                    recording = await _client.StopTeachRecordingAsync();
                }
                catch (Exception exception)
                {
                    AddLog("Warning", "Recorder", $"录制已由停止示教收尾：{exception.Message}");
                }
                finally
                {
                    IsTeachRecording = false;
                    FinishRecordingClock();
                }
            }

            if (IsTeachActive)
            {
                try
                {
                    var stopped = await _client.StopTeachAsync();
                    if (!stopped.Accepted)
                    {
                        AddLog("Warning", "Teach", "后端报告拖动示教已提前结束");
                    }
                }
                finally
                {
                    IsTeachActive = false;
                }
            }

            if (recording is not null)
            {
                ApplySavedRecording(recording);
            }
            TeachStatus = "示教录制已停止并保存";
            await RefreshTeachRecordingsAsync();
            if (!string.IsNullOrWhiteSpace(RecordingPath))
            {
                SelectedTeachRecording = TeachRecordings.FirstOrDefault(
                    item => item.Path.Equals(RecordingPath, StringComparison.Ordinal));
            }
            SelectedTeachRecording ??= TeachRecordings.FirstOrDefault();
            if (SelectedTeachRecording is not null)
            {
                ApplySavedRecording(SelectedTeachRecording);
            }
            AddLog("Info", "Recorder", TeachRecordingSummary);
            TeachRecordingName = NextRecordingName();
        });
    }

    private bool CanStopTeachSession() => HasControl && (IsTeachActive || IsTeachRecording) && !IsBusy;

    [RelayCommand(CanExecute = nameof(CanStartTeach))]
    private async Task StartTeachAsync()
    {
        await RunBusyAsync("Teach", async () =>
        {
            var result = await _client.StartTeachAsync();
            IsTeachActive = result.Accepted;
            TeachStatus = result.Accepted ? "拖动示教中" : $"拒绝：{result.RejectReason}";
            AddLog(result.Accepted ? "Info" : "Warning", "Teach", TeachStatus);
        });
    }

    private bool CanStartTeach() => CanControl && !IsTeachActive;

    [RelayCommand(CanExecute = nameof(CanStopTeach))]
    private async Task StopTeachAsync()
    {
        try
        {
            var wasRecording = IsTeachRecording;
            TeachRecordingSnapshot? recording = null;
            if (wasRecording)
            {
                try
                {
                    recording = await _client.StopTeachRecordingAsync();
                }
                catch (Exception exception)
                {
                    AddLog("Warning", "Recorder", $"录制已由停止示教收尾：{exception.Message}");
                }
                finally
                {
                    IsTeachRecording = false;
                    FinishRecordingClock();
                }
                if (recording is not null)
                {
                    ApplySavedRecording(recording);
                }
                await RefreshTeachRecordingsAsync();
            }
            var result = await _client.StopTeachAsync();
            IsTeachActive = false;
            TeachStatus = result.Accepted ? "已安全停止" : "未在示教";
            AddLog(result.Accepted ? "Info" : "Warning", "Teach", TeachStatus);
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Teach", exception);
        }
        finally
        {
            NotifyMotionCommands();
        }
    }

    private bool CanStopTeach() => HasControl && IsTeachActive;

    [RelayCommand(CanExecute = nameof(CanStartTeachRecording))]
    private async Task StartTeachRecordingAsync()
    {
        try
        {
            var requestedPath = RecordingRequestPath(TeachRecordingName);
            BeginTeachRecording(await _client.StartTeachRecordingAsync(requestedPath));
            TeachStatus = "正在同步录制";
            AddLog("Info", "Recorder", RecordingPath);
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Recorder", exception);
        }
    }

    private bool CanStartTeachRecording() => HasControl && IsTeachActive && !IsTeachRecording;

    [RelayCommand(CanExecute = nameof(CanStopTeachRecording))]
    private async Task StopTeachRecordingAsync()
    {
        try
        {
            var recording = await _client.StopTeachRecordingAsync();
            IsTeachRecording = false;
            FinishRecordingClock();
            TeachStatus = IsTeachActive ? "拖动示教中" : "已停止";
            if (recording is not null)
            {
                ApplySavedRecording(recording);
                AddLog("Info", "Recorder", $"已保存 {recording.FrameCount:N0} frames · {recording.Path}");
            }
            await RefreshTeachRecordingsAsync();
        }
        catch (Exception exception)
        {
            HandleOperationFailure("Recorder", exception);
        }
    }

    private bool CanStopTeachRecording() => HasControl && IsTeachRecording;

    private void BeginTeachRecording(string path)
    {
        RecordingPath = path;
        TeachRecordingFrameCount = 0;
        TeachRecordingElapsedSeconds = 0;
        _recordingStartedAt = DateTimeOffset.UtcNow;
        IsTeachRecording = true;
    }

    private void FinishRecordingClock()
    {
        if (_recordingStartedAt is not null)
        {
            TeachRecordingElapsedSeconds = Math.Max(
                TeachRecordingElapsedSeconds,
                (DateTimeOffset.UtcNow - _recordingStartedAt.Value).TotalSeconds);
        }
        _recordingStartedAt = null;
    }

    private void ApplySavedRecording(TeachRecordingSnapshot recording)
    {
        RecordingPath = recording.Path;
        TeachRecordingFrameCount = recording.FrameCount;
        if (recording.DurationSeconds > 0)
        {
            TeachRecordingElapsedSeconds = recording.DurationSeconds;
        }
    }

    private static string RecordingRequestPath(string name)
    {
        var stem = name.Trim();
        if (stem.EndsWith(".jsonl", StringComparison.OrdinalIgnoreCase))
        {
            stem = stem[..^6];
        }
        var normalized = string.Concat(stem.Select(character =>
            char.IsLetterOrDigit(character) || character is '-' or '_' ? character : '_'));
        if (string.IsNullOrWhiteSpace(normalized))
        {
            normalized = NextRecordingName();
        }
        return $"{normalized}.jsonl";
    }

    private static string NextRecordingName() => $"demo_{DateTime.Now:yyyyMMdd_HHmmss}";

    [RelayCommand]
    private async Task RefreshTeachRecordingsAsync()
    {
        try
        {
            var selectedPath = SelectedTeachRecording?.Path;
            var recordings = await _client.ListTeachRecordingsAsync();
            TeachRecordings.Clear();
            foreach (var recording in recordings.OrderByDescending(recording => recording.RecordedAt))
            {
                TeachRecordings.Add(recording);
            }
            SelectedTeachRecording = TeachRecordings.FirstOrDefault(recording => recording.Path == selectedPath)
                ?? TeachRecordings.FirstOrDefault();
        }
        catch (Exception exception)
        {
            AddLog("Warning", "Teach", $"读取轨迹列表失败：{exception.Message}");
        }
    }

    [RelayCommand(CanExecute = nameof(CanPlayTeachRecording))]
    private async Task PlayTeachRecordingAsync()
    {
        var recording = SelectedTeachRecording;
        if (recording is null)
        {
            return;
        }
        await RunBusyAsync("Teach playback", async () =>
        {
            var handle = await _client.PlayTeachRecordingAsync(recording.Path);
            _activeExecutionId = handle.ExecutionId;
            ExecutionStatus = "示教回放中";
            await foreach (var progress in _client.StreamExecutionAsync(handle.ExecutionId))
            {
                ExecutionFraction = progress.Fraction;
                ExecutionStatus = progress.State switch
                {
                    ExecutionState.Done => "回放完成",
                    ExecutionState.Cancelled => "回放已取消",
                    ExecutionState.Failed => $"回放失败：{progress.ErrorMessage}",
                    _ => "示教回放中",
                };
            }
            AddLog(ExecutionStatus == "回放完成" ? "Info" : "Warning", "Teach", ExecutionStatus);
            _activeExecutionId = string.Empty;
        });
    }

    private bool CanPlayTeachRecording() => CanControl && !IsTeachActive && SelectedTeachRecording is not null;

    [RelayCommand(CanExecute = nameof(CanExportDataset))]
    private async Task ExportDatasetAsync()
    {
        var recording = SelectedTeachRecording;
        if (recording is null)
        {
            return;
        }
        IsDatasetBusy = true;
        DatasetProgress = 0;
        DatasetStatus = "QUEUED";
        DatasetResult = string.Empty;
        try
        {
            var handle = await _client.ExportLeRobotAsync(
                recording.Path,
                DatasetOutputDirectory,
                DatasetRepoId,
                DatasetTask);
            _activeDatasetJobId = handle.JobId;
            CancelDatasetExportCommand.NotifyCanExecuteChanged();
            await foreach (var job in _client.WatchDatasetJobAsync(handle.JobId))
            {
                DatasetProgress = Math.Clamp(job.Progress * 100.0, 0, 100);
                DatasetStatus = job.State.ToString().ToUpperInvariant();
                DatasetResult = job.State == DatasetExportState.Done
                    ? $"{job.OutputDirectory} · {job.FrameCount:N0} frames"
                    : job.ErrorMessage;
            }
            AddLog(DatasetStatus == "DONE" ? "Info" : "Warning", "Dataset",
                string.IsNullOrWhiteSpace(DatasetResult) ? DatasetStatus : DatasetResult);
        }
        catch (Exception exception)
        {
            DatasetStatus = "FAILED";
            DatasetResult = exception.Message;
            AddLog("Error", "Dataset", exception.Message);
        }
        finally
        {
            _activeDatasetJobId = string.Empty;
            IsDatasetBusy = false;
            CancelDatasetExportCommand.NotifyCanExecuteChanged();
        }
    }

    private bool CanExportDataset() => SelectedTeachRecording is not null && !IsDatasetBusy;

    [RelayCommand(CanExecute = nameof(CanCancelDatasetExport))]
    private async Task CancelDatasetExportAsync()
    {
        if (string.IsNullOrWhiteSpace(_activeDatasetJobId))
        {
            return;
        }
        var cancelled = await _client.CancelDatasetJobAsync(_activeDatasetJobId);
        AddLog(cancelled ? "Info" : "Warning", "Dataset", cancelled ? "已请求取消导出" : "导出已结束");
    }

    private bool CanCancelDatasetExport() => IsDatasetBusy && !string.IsNullOrWhiteSpace(_activeDatasetJobId);

    private void SetUiScale(double scale)
    {
        UiScale = Math.Clamp(Math.Round(scale * 20) / 20, 0.90, 1.40);
        Settings = Settings with { UiScale = UiScale, JogSpeed = JogSpeed };
        _settingsStore.Save(Settings);
    }

    private async Task RunEnvironmentAsync(bool run)
    {
        if (IsEnvironmentBusy)
        {
            return;
        }
        IsEnvironmentBusy = true;
        EnvironmentSteps.Clear();
        try
        {
            var result = run
                ? await _environmentGuide.RunAsync(Settings, CancellationToken.None)
                : await _environmentGuide.ProbeAsync(Settings, CancellationToken.None);
            foreach (var step in result.Steps)
            {
                EnvironmentSteps.Add(step);
                AddLog(step.Success ? "Info" : "Warning", "Environment", $"{step.Name}：{step.Detail}");
            }
            if (run && result.Success)
            {
                await InitializeAsync();
            }
        }
        catch (Exception exception)
        {
            AddLog("Error", "Environment", exception.Message);
        }
        finally
        {
            IsEnvironmentBusy = false;
        }
    }

    private async Task PumpJogAsync(int jointIndex, double direction, CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested && _jogChannel is not null)
            {
                var velocity = new double[6];
                velocity[jointIndex] = direction * JogSpeed;
                await _jogChannel.Writer.WriteAsync(velocity, cancellationToken);
                await Task.Delay(TimeSpan.FromMilliseconds(50), cancellationToken);
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private async Task StopJogCoreAsync()
    {
        Exception? failure = null;
        var jogCallTask = _jogCallTask;
        var jogCallLifetime = _jogCallLifetime;
        var jogCallStillRunning = false;
        _jogPumpLifetime?.Cancel();
        if (_jogPumpTask is not null)
        {
            failure = await ObserveCompletionAsync(_jogPumpTask) ?? failure;
        }
        if (_jogChannel is not null)
        {
            try
            {
                await _jogChannel.Writer.WriteAsync(new double[6]);
                await Task.Delay(60);
            }
            catch (Exception exception) when (exception is not OperationCanceledException)
            {
                failure ??= exception;
            }
            _jogChannel.Writer.TryComplete();
        }
        if (jogCallTask is not null)
        {
            var completed = await Task.WhenAny(jogCallTask, Task.Delay(500));
            if (completed != jogCallTask)
            {
                jogCallLifetime?.Cancel();
                completed = await Task.WhenAny(jogCallTask, Task.Delay(750));
            }
            if (completed == jogCallTask)
            {
                failure = await ObserveCompletionAsync(jogCallTask) ?? failure;
            }
            else
            {
                jogCallStillRunning = true;
                failure ??= new TimeoutException("点动传输未及时退出，已交由服务端新鲜度窗口停止");
                _ = ObserveCompletionAsync(jogCallTask);
            }
        }
        _jogPumpLifetime?.Dispose();
        if (jogCallStillRunning && jogCallTask is not null)
        {
            _ = jogCallTask.ContinueWith(
                _ => jogCallLifetime?.Dispose(),
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }
        else
        {
            jogCallLifetime?.Dispose();
        }
        _jogPumpLifetime = null;
        _jogCallLifetime = null;
        _jogChannel = null;
        _jogPumpTask = null;
        _jogCallTask = null;
        IsJogging = false;
        if (failure is not null)
        {
            ConnectionState = _client.ConnectionState;
            AddLog("Warning", "Jog", $"点动通道中断，已安全停止：{failure.Message}");
            ReconcileControlLease();
        }
    }

    private async Task MonitorJogAsync(Task jogCallTask)
    {
        var failure = await ObserveCompletionAsync(jogCallTask);
        if (failure is null)
        {
            return;
        }
        await _jogGate.WaitAsync();
        try
        {
            if (!ReferenceEquals(_jogCallTask, jogCallTask))
            {
                return;
            }
            await StopJogCoreAsync();
        }
        finally
        {
            _jogGate.Release();
        }
    }

    private async Task RefreshPoseAsync(IReadOnlyList<double> joints)
    {
        if (Interlocked.Exchange(ref _poseRefreshActive, 1) != 0)
        {
            return;
        }
        try
        {
            var position = await _client.ForwardKinematicsAsync(joints);
            if (position.Count >= 3)
            {
                TcpX = position[0];
                TcpY = position[1];
                TcpZ = position[2];
                if (!_cartesianTargetInitialized)
                {
                    TargetX = TcpX;
                    TargetY = TcpY;
                    TargetZ = TcpZ;
                    _cartesianTargetInitialized = true;
                }
            }
        }
        catch
        {
        }
        finally
        {
            Interlocked.Exchange(ref _poseRefreshActive, 0);
        }
    }

    private async Task RunBusyAsync(string operation, Func<Task> action)
    {
        if (IsBusy)
        {
            return;
        }
        IsBusy = true;
        NotifyMotionCommands();
        try
        {
            await action();
        }
        catch (Exception exception)
        {
            HandleOperationFailure(operation, exception);
        }
        finally
        {
            IsBusy = false;
            NotifyMotionCommands();
        }
    }

    private void NotifyMotionCommands()
    {
        OnPropertyChanged(nameof(CanControl));
        MoveJCommand.NotifyCanExecuteChanged();
        MoveLCommand.NotifyCanExecuteChanged();
        GripperOpenCommand.NotifyCanExecuteChanged();
        GripperCloseCommand.NotifyCanExecuteChanged();
        ResetArmCommand.NotifyCanExecuteChanged();
        ToggleDemoSequenceCommand.NotifyCanExecuteChanged();
        StartTeachSessionCommand.NotifyCanExecuteChanged();
        StopTeachSessionCommand.NotifyCanExecuteChanged();
        StartTeachCommand.NotifyCanExecuteChanged();
        StopTeachCommand.NotifyCanExecuteChanged();
        StartTeachRecordingCommand.NotifyCanExecuteChanged();
        StopTeachRecordingCommand.NotifyCanExecuteChanged();
        PlayTeachRecordingCommand.NotifyCanExecuteChanged();
    }

    private void RequestDemoStop(bool logRequest = false)
    {
        var lifetime = _demoLifetime;
        if (lifetime is null || lifetime.IsCancellationRequested)
        {
            return;
        }
        lifetime.Cancel();
        if (logRequest)
        {
            AddLog("Info", "Demo", "已请求停止，当前 MoveJ 段完成后退出循环");
        }
    }

    private void SetJointTargets(IReadOnlyList<double> positions)
    {
        if (positions.Count != 6)
        {
            throw new ArgumentException("关节目标必须包含 6 个数值", nameof(positions));
        }
        TargetJ1 = positions[0];
        TargetJ2 = positions[1];
        TargetJ3 = positions[2];
        TargetJ4 = positions[3];
        TargetJ5 = positions[4];
        TargetJ6 = positions[5];
    }

    private void ReconcileControlLease()
    {
        if (!HasControl || _client.HasActiveLease)
        {
            return;
        }
        HasControl = false;
        ControlHolder = "无";
        AddLog("Warning", "Control", "控制权 lease 已失效，请重新获取控制权");
    }

    private void HandleOperationFailure(string operation, Exception exception)
    {
        ReconcileControlLease();
        AddLog("Error", operation, exception.Message);
    }

    private void AddLog(string level, string source, string message)
    {
        Logs.Add(new TerminalLogEntry(DateTimeOffset.Now, level, source, message));
        while (Logs.Count > 500)
        {
            Logs.RemoveAt(0);
        }
    }

    private static bool TryParseJog(string? parameter, out int jointIndex, out double direction)
    {
        jointIndex = -1;
        direction = 0;
        var parts = parameter?.Split(':');
        return parts?.Length == 2
            && int.TryParse(parts[0], out jointIndex)
            && jointIndex is >= 0 and < 6
            && double.TryParse(parts[1], out direction)
            && Math.Abs(direction) == 1;
    }

    private static async Task<Exception?> ObserveCompletionAsync(Task task)
    {
        try
        {
            await task;
            return null;
        }
        catch (OperationCanceledException)
        {
            return null;
        }
        catch (Exception exception)
        {
            return exception;
        }
    }

    partial void OnHasControlChanged(bool value)
    {
        NotifyMotionCommands();
        OnPropertyChanged(nameof(ControlSummary));
    }

    partial void OnEStopEngagedChanged(bool value)
    {
        if (value)
        {
            RequestDemoStop();
        }
        NotifyMotionCommands();
    }

    partial void OnConnectionStateChanged(TerminalConnectionState value)
    {
        NotifyMotionCommands();
        OnPropertyChanged(nameof(IsConnected));
        OnPropertyChanged(nameof(ConnectionLabel));
    }

    partial void OnControlHolderChanged(string value) => OnPropertyChanged(nameof(ControlSummary));

    partial void OnStateAgeMsChanged(long value) => OnPropertyChanged(nameof(LiveSummary));

    partial void OnGripperPositionChanged(double value)
    {
        OnPropertyChanged(nameof(GripperPercent));
        OnPropertyChanged(nameof(GripperOpeningState));
    }

    partial void OnGripperValidChanged(bool value)
    {
        OnPropertyChanged(nameof(GripperStatus));
        OnPropertyChanged(nameof(GripperOpeningState));
    }

    partial void OnGripperFaultChanged(uint value) => OnPropertyChanged(nameof(GripperStatus));

    partial void OnUiScaleChanged(double value) => OnPropertyChanged(nameof(UiScaleLabel));

    partial void OnIsTeachActiveChanged(bool value)
    {
        NotifyMotionCommands();
        OnPropertyChanged(nameof(TeachRecordingSummary));
    }

    partial void OnIsTeachRecordingChanged(bool value)
    {
        NotifyMotionCommands();
        OnPropertyChanged(nameof(TeachRecordingSummary));
    }

    partial void OnTeachRecordingElapsedSecondsChanged(double value)
    {
        OnPropertyChanged(nameof(TeachRecordingElapsedLabel));
        OnPropertyChanged(nameof(TeachRecordingSummary));
    }

    partial void OnTeachRecordingFrameCountChanged(long value) =>
        OnPropertyChanged(nameof(TeachRecordingSummary));

    partial void OnIsDemoRunningChanged(bool value)
    {
        OnPropertyChanged(nameof(DemoActionLabel));
        NotifyMotionCommands();
    }

    private sealed record DemoStep(
        string Name,
        IReadOnlyList<double> Positions,
        double DurationSeconds);
}
