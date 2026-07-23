namespace Panthera.Terminal.Core;

public interface IArmdClient : IAsyncDisposable
{
    TerminalConnectionState ConnectionState { get; }

    bool HasActiveLease => true;

    Task<DaemonSnapshot> GetDaemonStatusAsync(CancellationToken cancellationToken = default);

    Task<CameraSnapshot> GetCameraStatusAsync(
        CameraSourceKind source,
        CancellationToken cancellationToken = default);

    IAsyncEnumerable<CameraFrameSnapshot> StreamCameraFramesAsync(
        CameraSourceKind source,
        CameraStreamKind stream,
        double maxRateHz = 15,
        CancellationToken cancellationToken = default);

    Task<ControlSnapshot> GetControlStatusAsync(CancellationToken cancellationToken = default);

    Task<SoftLimitSnapshot> GetSoftLimitsAsync(CancellationToken cancellationToken = default);

    Task<ControlSnapshot> AcquireControlAsync(
        string clientId,
        bool force = false,
        CancellationToken cancellationToken = default);

    Task ReleaseControlAsync(CancellationToken cancellationToken = default);

    Task TriggerEStopAsync(string reason, CancellationToken cancellationToken = default);

    Task ClearEStopAsync(CancellationToken cancellationToken = default);

    IAsyncEnumerable<RobotSnapshot> StreamStateAsync(
        double rateHz = 60,
        CancellationToken cancellationToken = default);

    Task<JointMoveResult> MoveJAsync(
        IReadOnlyList<double> positions,
        double durationSeconds,
        bool wait,
        CancellationToken cancellationToken = default);

    Task JogAsync(
        IAsyncEnumerable<IReadOnlyList<double>> commands,
        CancellationToken cancellationToken = default);

    Task<OperationResult> GripperMoveAsync(
        double position,
        double velocity,
        CancellationToken cancellationToken = default);

    Task<ExecutionHandle> MoveLAsync(
        CartesianTarget target,
        double durationSeconds,
        CancellationToken cancellationToken = default);

    Task<ExecutionHandle> RunJointTrajectoryAsync(
        IReadOnlyList<JointTrajectoryWaypoint> waypoints,
        IReadOnlyList<double> durations,
        CancellationToken cancellationToken = default);

    IAsyncEnumerable<ExecutionProgress> StreamExecutionAsync(
        string executionId,
        CancellationToken cancellationToken = default);

    Task<bool> CancelExecutionAsync(
        string executionId,
        CancellationToken cancellationToken = default);

    Task<IReadOnlyList<double>> ForwardKinematicsAsync(
        IReadOnlyList<double> joints,
        CancellationToken cancellationToken = default);

    Task<OperationResult> StartTeachAsync(CancellationToken cancellationToken = default);

    Task<OperationResult> StopTeachAsync(CancellationToken cancellationToken = default);

    Task<string> StartTeachRecordingAsync(
        string path = "",
        CancellationToken cancellationToken = default);

    Task<TeachRecordingSnapshot?> StopTeachRecordingAsync(
        CancellationToken cancellationToken = default);

    Task<IReadOnlyList<TeachRecordingSnapshot>> ListTeachRecordingsAsync(
        CancellationToken cancellationToken = default);

    Task<ExecutionHandle> PlayTeachRecordingAsync(
        string path,
        CancellationToken cancellationToken = default);

    Task<DatasetJobHandle> ExportLeRobotAsync(
        string trajectoryPath,
        string outputDirectory,
        string repoId,
        string task,
        bool overwrite = false,
        CancellationToken cancellationToken = default);

    IAsyncEnumerable<DatasetJobSnapshot> WatchDatasetJobAsync(
        string jobId,
        CancellationToken cancellationToken = default);

    Task<bool> CancelDatasetJobAsync(
        string jobId,
        CancellationToken cancellationToken = default);
}

public interface ITerminalSettingsStore
{
    TerminalSettings Load();

    void Save(TerminalSettings settings);
}

public interface IEnvironmentGuideService
{
    Task<EnvironmentGuideResult> ProbeAsync(TerminalSettings settings, CancellationToken cancellationToken);

    Task<EnvironmentGuideResult> RunAsync(TerminalSettings settings, CancellationToken cancellationToken);
}

public interface IRemoteDeploymentService
{
    Task<RemoteDeploymentReport> ConfigureAndStartAsync(
        SshConnectionSettings settings,
        IProgress<RemoteDeploymentProgress>? progress = null,
        CancellationToken cancellationToken = default);
}

public interface ISshConnectionDiscoveryService
{
    Task<IReadOnlyList<SshConnectionCandidate>> DiscoverAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken = default);
}

public sealed record EnvironmentGuideStep(string Name, bool Success, string Detail, string Command);

public sealed record EnvironmentGuideResult(IReadOnlyList<EnvironmentGuideStep> Steps)
{
    public bool Success => Steps.Count > 0 && Steps.All(step => step.Success);
}
