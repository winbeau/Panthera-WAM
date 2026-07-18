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

    private sealed class FailingJogClient : IArmdClient
    {
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

        public Task<ControlSnapshot> GetControlStatusAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task<SoftLimitSnapshot> GetSoftLimitsAsync(CancellationToken cancellationToken = default) =>
            throw new NotSupportedException();

        public Task<ControlSnapshot> AcquireControlAsync(
            string clientId,
            bool force = false,
            CancellationToken cancellationToken = default) => throw new NotSupportedException();

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
