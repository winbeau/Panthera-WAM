using System.Collections.ObjectModel;
using System.Threading.Channels;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Panthera.Terminal.Core;

public sealed partial class MainWindowViewModel : ObservableObject
{
    private static readonly double[] JointMinimum = [-2.4, -0.1, -0.1, -1.6, -1.7, -2.5];
    private static readonly double[] JointMaximum = [2.4, 3.2, 4.0, 1.6, 1.7, 2.5];
    private readonly IArmdClient _client;
    private readonly IEnvironmentGuideService _environmentGuide;
    private readonly ITerminalSettingsStore _settingsStore;
    private readonly SemaphoreSlim _jogGate = new(1, 1);
    private CancellationTokenSource? _jogPumpLifetime;
    private CancellationTokenSource? _jogCallLifetime;
    private Channel<IReadOnlyList<double>>? _jogChannel;
    private Task? _jogPumpTask;
    private Task? _jogCallTask;
    private string _activeExecutionId = string.Empty;
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
        JogSpeed = settings.JogSpeed;
        TargetDuration = 3.0;
        ConnectionDetail = settings.Endpoint;
    }

    public TerminalSettings Settings { get; private set; }

    public ObservableCollection<JointGaugeViewModel> Joints { get; }

    public ObservableCollection<TerminalLogEntry> Logs { get; } = [];

    public ObservableCollection<EnvironmentGuideStep> EnvironmentSteps { get; } = [];

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(AcquireControlCommand))]
    private TerminalConnectionState _connectionState = TerminalConnectionState.Disconnected;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ReleaseControlCommand))]
    [NotifyCanExecuteChangedFor(nameof(MoveJCommand))]
    [NotifyCanExecuteChangedFor(nameof(MoveLCommand))]
    [NotifyCanExecuteChangedFor(nameof(GripperOpenCommand))]
    [NotifyCanExecuteChangedFor(nameof(GripperCloseCommand))]
    private bool _hasControl;

    [ObservableProperty]
    private string _controlHolder = "无";

    [ObservableProperty]
    private bool _eStopEngaged;

    [ObservableProperty]
    private string _daemonSummary = "等待连接";

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
    private double _targetDuration;

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
    private bool _isEnvironmentBusy;

    [ObservableProperty]
    private string _theme;

    public bool CanControl => HasControl && !EStopEngaged && ConnectionState == TerminalConnectionState.Connected && !IsBusy;

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

    public async Task InitializeAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            var daemon = await _client.GetDaemonStatusAsync(cancellationToken);
            var control = await _client.GetControlStatusAsync(cancellationToken);
            var limits = await _client.GetSoftLimitsAsync(cancellationToken);
            ConnectionState = _client.ConnectionState;
            HasControl = control.Held && control.HolderClientId == Environment.MachineName;
            ControlHolder = control.Held ? control.HolderClientId : "无";
            EStopEngaged = control.EStopEngaged;
            DaemonSummary = daemon.Simulation
                ? $"仿真 · {daemon.ControlHz:F0} Hz"
                : $"真机 {(daemon.HardwareConnected ? "在线" : "未连接")} · {daemon.ControlHz:F0} Hz";
            ConnectionDetail = Settings.Endpoint;
            _gripperMinimum = limits.GripperMinimum;
            _gripperMaximum = limits.GripperMaximum;
            OnPropertyChanged(nameof(GripperPercent));
            for (var index = 0; index < Math.Min(Joints.Count, limits.Joints.Count); index++)
            {
                Joints[index].Minimum = limits.Joints[index].Minimum;
                Joints[index].Maximum = limits.Joints[index].Maximum;
            }
            AddLog("Info", "Connection", $"已连接 {Settings.Endpoint}");
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
        await StopJogAsync();
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
            HasControl = status.Held;
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
        await StopJogAsync();
        await _client.ReleaseControlAsync();
        HasControl = false;
        ControlHolder = "无";
        AddLog("Info", "Control", "已释放控制权");
    }

    private bool CanReleaseControl() => HasControl;

    [RelayCommand]
    private async Task TriggerEStopAsync()
    {
        await StopJogAsync();
        await _client.TriggerEStopAsync("WPF operator E-STOP");
        EStopEngaged = true;
        AddLog("Fault", "Safety", "已触发急停");
    }

    [RelayCommand]
    private async Task ClearEStopAsync()
    {
        if (!HasControl)
        {
            AddLog("Warning", "Safety", "复位急停前需获取控制权");
            return;
        }
        await _client.ClearEStopAsync();
        EStopEngaged = false;
        AddLog("Info", "Safety", "急停已复位");
    }

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private async Task MoveJAsync()
    {
        await RunBusyAsync("MoveJ", async () =>
        {
            var result = await _client.MoveJAsync(
                [TargetJ1, TargetJ2, TargetJ3, TargetJ4, TargetJ5, TargetJ6],
                TargetDuration,
                true);
            AddLog(result.Accepted && result.Reached ? "Info" : "Warning", "Motion",
                result.Accepted ? $"MoveJ 完成，到位={result.Reached}" : result.RejectReason);
        });
    }

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
                TargetDuration);
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
        var cancelled = await _client.CancelExecutionAsync(_activeExecutionId);
        AddLog(cancelled ? "Info" : "Warning", "Motion", cancelled ? "已请求平滑取消" : "执行已结束或不存在");
    }

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private Task GripperOpenAsync() => RunGripperAsync(1.6, "打开");

    [RelayCommand(CanExecute = nameof(CanRunMotion))]
    private Task GripperCloseAsync() => RunGripperAsync(0.0, "闭合");

    private async Task RunGripperAsync(double position, string label)
    {
        var result = await _client.GripperMoveAsync(position, 0.35);
        AddLog(result.Accepted ? "Info" : "Warning", "Gripper",
            result.Accepted ? $"夹爪{label}指令已接受" : result.RejectReason);
    }

    private bool CanRunMotion() => CanControl;

    [RelayCommand]
    private async Task StartJogAsync(string? parameter)
    {
        if (!CanControl || !TryParseJog(parameter, out var jointIndex, out var direction))
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
            AddLog("Error", "Jog", $"点动启动失败：{exception.Message}");
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
        if (_jogCallTask is not null)
        {
            var completed = await Task.WhenAny(_jogCallTask, Task.Delay(500));
            if (completed != _jogCallTask)
            {
                _jogCallLifetime?.Cancel();
            }
            failure = await ObserveCompletionAsync(_jogCallTask) ?? failure;
        }
        _jogPumpLifetime?.Dispose();
        _jogCallLifetime?.Dispose();
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
            AddLog("Error", operation, exception.Message);
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

    partial void OnEStopEngagedChanged(bool value) => NotifyMotionCommands();

    partial void OnConnectionStateChanged(TerminalConnectionState value)
    {
        NotifyMotionCommands();
        OnPropertyChanged(nameof(IsConnected));
        OnPropertyChanged(nameof(ConnectionLabel));
    }

    partial void OnControlHolderChanged(string value) => OnPropertyChanged(nameof(ControlSummary));

    partial void OnStateAgeMsChanged(long value) => OnPropertyChanged(nameof(LiveSummary));

    partial void OnGripperPositionChanged(double value) => OnPropertyChanged(nameof(GripperPercent));

    partial void OnGripperValidChanged(bool value) => OnPropertyChanged(nameof(GripperStatus));

    partial void OnGripperFaultChanged(uint value) => OnPropertyChanged(nameof(GripperStatus));
}
