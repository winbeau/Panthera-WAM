using System.Runtime.CompilerServices;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Tests;

public sealed class MainWindowViewModelTests
{
    [Fact]
    public void UiScale_IsClampedToReadableMinimum()
    {
        var viewModel = new MainWindowViewModel(
            new FailingJogClient(),
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings(UiScale: 0.5));

        Assert.Equal(0.90, viewModel.UiScale);
    }

    [Fact]
    public async Task JogTransportFailure_IsContainedAndResetsJogState()
    {
        var client = new FailingJogClient();
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        await viewModel.StartJogCommand.ExecuteAsync("0:1");
        var deadline = DateTime.UtcNow.AddSeconds(2);
        while (viewModel.IsJogging && DateTime.UtcNow < deadline)
        {
            await Task.Delay(20);
        }

        Assert.False(viewModel.IsJogging);
        Assert.Equal(TerminalConnectionState.Disconnected, viewModel.ConnectionState);
        Assert.Contains(viewModel.Logs, entry =>
            entry.Source == "Jog" && entry.Message.Contains("已安全停止", StringComparison.Ordinal));
    }

    [Fact]
    public async Task ClearEStopFailure_IsContainedAndKeepsEStopLatched()
    {
        var viewModel = new MainWindowViewModel(
            new FailingJogClient(),
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            EStopEngaged = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        await viewModel.ClearEStopCommand.ExecuteAsync(null);

        Assert.True(viewModel.EStopEngaged);
        Assert.Contains(viewModel.Logs, entry =>
            entry.Source == "Safety"
            && entry.Message.Contains("急停复位失败，保持急停", StringComparison.Ordinal));
    }

    [Fact]
    public async Task TeachSession_AcquiresControlRecordsSavesAndSelectsTrajectory()
    {
        var client = new FailingJogClient(enableTeachSession: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            ConnectionState = TerminalConnectionState.Connected,
            TeachRecordingName = "pick_demo",
        };

        await viewModel.StartTeachSessionCommand.ExecuteAsync(null);

        Assert.True(viewModel.HasControl);
        Assert.True(viewModel.IsTeachActive);
        Assert.True(viewModel.IsTeachRecording);
        Assert.EndsWith("pick_demo.jsonl", viewModel.RecordingPath, StringComparison.Ordinal);

        await viewModel.StopTeachSessionCommand.ExecuteAsync(null);

        Assert.False(viewModel.IsTeachActive);
        Assert.False(viewModel.IsTeachRecording);
        Assert.Equal(240, viewModel.TeachRecordingFrameCount);
        Assert.Single(viewModel.TeachRecordings);
        Assert.Equal(viewModel.RecordingPath, viewModel.SelectedTeachRecording?.Path);
        Assert.Equal(
            new[] { "acquire", "teach-start", "record-start", "record-stop", "teach-stop", "list" },
            client.TeachCalls);
    }

    [Fact]
    public async Task TeachSession_ReacquiresWhenDisplayedControlLeaseIsStale()
    {
        var client = new FailingJogClient(enableTeachSession: true, hasActiveLease: false);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
            TeachRecordingName = "stale_lease_demo",
        };

        await viewModel.StartTeachSessionCommand.ExecuteAsync(null);

        Assert.True(viewModel.HasControl);
        Assert.True(viewModel.IsTeachActive);
        Assert.True(viewModel.IsTeachRecording);
        Assert.Equal("acquire", client.TeachCalls[0]);
    }

    [Fact]
    public async Task GripperLeaseFailure_IsContainedAndClearsDisplayedControl()
    {
        var client = new FailingJogClient(failGripperWithLostLease: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        var exception = await Record.ExceptionAsync(() => viewModel.GripperOpenCommand.ExecuteAsync(null));

        Assert.Null(exception);
        Assert.False(viewModel.HasControl);
        Assert.Contains(viewModel.Logs, entry =>
            entry.Source == "Control"
            && entry.Message.Contains("lease 已失效", StringComparison.Ordinal));
        Assert.Contains(viewModel.Logs, entry => entry.Source == "Gripper");
    }

    [Fact]
    public async Task JogStop_IsBoundedWhenTransportIgnoresCancellation()
    {
        var client = new FailingJogClient(hangJog: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        await viewModel.StartJogCommand.ExecuteAsync("0:1");
        await client.JogStarted.Task.WaitAsync(TimeSpan.FromSeconds(1));
        await viewModel.StopJogAsync().WaitAsync(TimeSpan.FromSeconds(3));

        Assert.False(viewModel.IsJogging);
        Assert.Contains(viewModel.Logs, entry =>
            entry.Source == "Jog"
            && entry.Message.Contains("新鲜度窗口停止", StringComparison.Ordinal));
        client.ReleaseHungJog();
    }

    [Fact]
    public async Task MoveL_ForwardsSixDimensionalTargetWhenOrientationIsEnabled()
    {
        var client = new FailingJogClient(enableMotion: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
            TargetX = 0.31,
            TargetY = -0.04,
            TargetZ = 0.28,
            TargetRoll = 0.1,
            TargetPitch = 0.2,
            TargetYaw = -0.3,
            PreserveOrientation = false,
        };

        await viewModel.MoveLCommand.ExecuteAsync(null);

        Assert.Equal(
            new CartesianTarget(0.31, -0.04, 0.28, 0.1, 0.2, -0.3, false),
            client.LastMoveLTarget);
    }

    [Fact]
    public async Task ResetArm_UsesSdkSafeHomePosition()
    {
        var client = new FailingJogClient(enableMotion: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        await viewModel.ResetArmCommand.ExecuteAsync(null);

        Assert.Equal(new[] { 0.0, 0.6, 0.6, 0.0, 0.0, 0.0 }, client.LastMoveJPositions);
        Assert.Equal(3.0, client.LastMoveJDuration);
        Assert.Contains(viewModel.Logs, entry =>
            entry.Source == "Motion" && entry.Message.Contains("安全姿态", StringComparison.Ordinal));
    }

    [Fact]
    public async Task DemoSequence_CanBeStoppedAfterCurrentMoveJSegment()
    {
        var client = new FailingJogClient(enableMotion: true, hangMoveJ: true);
        var viewModel = new MainWindowViewModel(
            client,
            new StubEnvironmentGuideService(),
            new StubSettingsStore(),
            new TerminalSettings())
        {
            HasControl = true,
            ConnectionState = TerminalConnectionState.Connected,
        };

        var demoTask = viewModel.ToggleDemoSequenceCommand.ExecuteAsync(null);
        await client.MoveJStarted.Task.WaitAsync(TimeSpan.FromSeconds(1));

        Assert.True(viewModel.IsDemoRunning);
        Assert.Equal("停止展示", viewModel.DemoActionLabel);
        Assert.Equal(new[] { 0.0, 0.6, 0.6, 0.0, 0.0, 0.0 }, client.LastMoveJPositions);

        await viewModel.ToggleDemoSequenceCommand.ExecuteAsync(null);
        client.ReleaseHungMoveJ();
        await demoTask.WaitAsync(TimeSpan.FromSeconds(2));

        Assert.False(viewModel.IsDemoRunning);
        Assert.Equal("展示动作", viewModel.DemoActionLabel);
    }

    private sealed class FailingJogClient : IArmdClient
    {
        private readonly bool _enableTeachSession;
        private readonly bool _enableMotion;
        private readonly bool _failGripperWithLostLease;
        private readonly bool _hangJog;
        private readonly bool _hangMoveJ;
        private readonly List<TeachRecordingSnapshot> _recordings = [];
        private string _recordingPath = string.Empty;
        private readonly TaskCompletionSource _releaseHungJog = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _releaseHungMoveJ = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public FailingJogClient(
            bool enableTeachSession = false,
            bool enableMotion = false,
            bool failGripperWithLostLease = false,
            bool hangJog = false,
            bool hangMoveJ = false,
            bool hasActiveLease = true)
        {
            _enableTeachSession = enableTeachSession;
            _enableMotion = enableMotion;
            _failGripperWithLostLease = failGripperWithLostLease;
            _hangJog = hangJog;
            _hangMoveJ = hangMoveJ;
            HasActiveLease = hasActiveLease;
        }

        public List<string> TeachCalls { get; } = [];

        public TaskCompletionSource JogStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public TaskCompletionSource MoveJStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public CartesianTarget? LastMoveLTarget { get; private set; }

        public IReadOnlyList<double>? LastMoveJPositions { get; private set; }

        public double LastMoveJDuration { get; private set; }

        public TerminalConnectionState ConnectionState { get; private set; } = TerminalConnectionState.Connected;

        public bool HasActiveLease { get; private set; }

        public void ReleaseHungJog() => _releaseHungJog.TrySetResult();

        public void ReleaseHungMoveJ() => _releaseHungMoveJ.TrySetResult();

        public async Task JogAsync(
            IAsyncEnumerable<IReadOnlyList<double>> commands,
            CancellationToken cancellationToken = default)
        {
            await foreach (var _ in commands.WithCancellation(cancellationToken))
            {
                if (_hangJog)
                {
                    JogStarted.TrySetResult();
                    await _releaseHungJog.Task;
                    return;
                }
                ConnectionState = TerminalConnectionState.Disconnected;
                throw new IOException("simulated transport failure");
            }
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;

        public Task<DaemonSnapshot> GetDaemonStatusAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task<CameraSnapshot> GetCameraStatusAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public async IAsyncEnumerable<CameraFrameSnapshot> StreamCameraFramesAsync(
            CameraStreamKind stream,
            double maxRateHz = 15,
            [EnumeratorCancellation]
            CancellationToken cancellationToken = default)
        {
            await Task.CompletedTask;
            yield break;
        }

        public Task<ControlSnapshot> GetControlStatusAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task<SoftLimitSnapshot> GetSoftLimitsAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task<ControlSnapshot> AcquireControlAsync(
            string clientId,
            bool force = false,
            CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("acquire");
            HasActiveLease = true;
            return Task.FromResult(new ControlSnapshot(true, clientId, true, false));
        }

        public Task ReleaseControlAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task TriggerEStopAsync(string reason, CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task ClearEStopAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public async IAsyncEnumerable<RobotSnapshot> StreamStateAsync(
            double rateHz = 60,
            [EnumeratorCancellation]
            CancellationToken cancellationToken = default)
        {
            await Task.CompletedTask;
            yield break;
        }

        public async Task<JointMoveResult> MoveJAsync(
            IReadOnlyList<double> positions,
            double durationSeconds,
            bool wait,
            CancellationToken cancellationToken = default)
        {
            if (!_enableMotion)
            {
                throw new NotSupportedException();
            }
            LastMoveJPositions = positions.ToArray();
            LastMoveJDuration = durationSeconds;
            MoveJStarted.TrySetResult();
            if (_hangMoveJ)
            {
                await _releaseHungMoveJ.Task;
            }
            return new JointMoveResult(true, true, Enumerable.Repeat(0.0, 6).ToArray(), "");
        }

        public Task<OperationResult> GripperMoveAsync(
            double position,
            double velocity,
            CancellationToken cancellationToken = default)
        {
            if (_failGripperWithLostLease)
            {
                HasActiveLease = false;
                throw new IOException("缺少或无效的控制权 lease");
            }
            if (_enableMotion)
            {
                return Task.FromResult(new OperationResult(true));
            }
            throw new NotSupportedException();
        }

        public Task<ExecutionHandle> MoveLAsync(
            CartesianTarget target,
            double durationSeconds,
            CancellationToken cancellationToken = default)
        {
            if (!_enableMotion)
            {
                throw new NotSupportedException();
            }
            LastMoveLTarget = target;
            return Task.FromResult(new ExecutionHandle("move-l-test"));
        }

        public async IAsyncEnumerable<ExecutionProgress> StreamExecutionAsync(
            string executionId,
            [EnumeratorCancellation]
            CancellationToken cancellationToken = default)
        {
            await Task.CompletedTask;
            yield break;
        }

        public Task<bool> CancelExecutionAsync(
            string executionId,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public Task<IReadOnlyList<double>> ForwardKinematicsAsync(
            IReadOnlyList<double> joints,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public Task<OperationResult> StartTeachAsync(CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("teach-start");
            return Task.FromResult(new OperationResult(true));
        }

        public Task<OperationResult> StopTeachAsync(CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("teach-stop");
            return Task.FromResult(new OperationResult(true));
        }

        public Task<string> StartTeachRecordingAsync(
            string path = "",
            CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("record-start");
            _recordingPath = $"/home/panthera/teach/{path}";
            return Task.FromResult(_recordingPath);
        }

        public Task<TeachRecordingSnapshot?> StopTeachRecordingAsync(
            CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("record-stop");
            var recording = new TeachRecordingSnapshot(_recordingPath, DateTimeOffset.Now, 1.2, 240);
            _recordings.Add(recording);
            return Task.FromResult<TeachRecordingSnapshot?>(recording);
        }

        public Task<IReadOnlyList<TeachRecordingSnapshot>> ListTeachRecordingsAsync(
            CancellationToken cancellationToken = default)
        {
            if (!_enableTeachSession)
            {
                throw new NotSupportedException();
            }
            TeachCalls.Add("list");
            return Task.FromResult<IReadOnlyList<TeachRecordingSnapshot>>(_recordings.ToArray());
        }

        public Task<ExecutionHandle> PlayTeachRecordingAsync(
            string path,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public Task<DatasetJobHandle> ExportLeRobotAsync(
            string trajectoryPath,
            string outputDirectory,
            string repoId,
            string task,
            bool overwrite = false,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public async IAsyncEnumerable<DatasetJobSnapshot> WatchDatasetJobAsync(
            string jobId,
            [EnumeratorCancellation]
            CancellationToken cancellationToken = default)
        {
            await Task.CompletedTask;
            yield break;
        }

        public Task<bool> CancelDatasetJobAsync(
            string jobId,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();
    }

    private sealed class StubEnvironmentGuideService : IEnvironmentGuideService
    {
        public Task<EnvironmentGuideResult> ProbeAsync(
            TerminalSettings settings,
            CancellationToken cancellationToken) =>
            Task.FromResult(new EnvironmentGuideResult([]));

        public Task<EnvironmentGuideResult> RunAsync(
            TerminalSettings settings,
            CancellationToken cancellationToken) =>
            Task.FromResult(new EnvironmentGuideResult([]));
    }

    private sealed class StubSettingsStore : ITerminalSettingsStore
    {
        public TerminalSettings Load() => new();

        public void Save(TerminalSettings settings)
        {
        }
    }
}
