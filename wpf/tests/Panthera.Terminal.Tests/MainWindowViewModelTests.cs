using System.Runtime.CompilerServices;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Tests;

public sealed class MainWindowViewModelTests
{
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
        Assert.True(viewModel.RecordingPath.EndsWith("pick_demo.jsonl", StringComparison.Ordinal));

        await viewModel.StopTeachSessionCommand.ExecuteAsync(null);

        Assert.False(viewModel.IsTeachActive);
        Assert.False(viewModel.IsTeachRecording);
        Assert.Equal(240, viewModel.TeachRecordingFrameCount);
        Assert.Single(viewModel.TeachRecordings);
        Assert.Equal(viewModel.RecordingPath, viewModel.SelectedTeachRecording?.Path);
        Assert.Equal(
            new[] { "acquire", "teach-start", "record-start", "teach-stop", "record-stop", "list" },
            client.TeachCalls);
    }

    private sealed class FailingJogClient : IArmdClient
    {
        private readonly bool _enableTeachSession;
        private readonly List<TeachRecordingSnapshot> _recordings = [];
        private string _recordingPath = string.Empty;

        public FailingJogClient(bool enableTeachSession = false)
        {
            _enableTeachSession = enableTeachSession;
        }

        public List<string> TeachCalls { get; } = [];

        public TerminalConnectionState ConnectionState { get; private set; } = TerminalConnectionState.Connected;

        public async Task JogAsync(
            IAsyncEnumerable<IReadOnlyList<double>> commands,
            CancellationToken cancellationToken = default)
        {
            await foreach (var _ in commands.WithCancellation(cancellationToken))
            {
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

        public Task<JointMoveResult> MoveJAsync(
            IReadOnlyList<double> positions,
            double durationSeconds,
            bool wait,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public Task<OperationResult> GripperMoveAsync(
            double position,
            double velocity,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

        public Task<ExecutionHandle> MoveLAsync(
            CartesianTarget target,
            double durationSeconds,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

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
