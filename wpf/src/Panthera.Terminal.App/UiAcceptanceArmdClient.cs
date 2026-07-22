using System.IO;
using System.Runtime.CompilerServices;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

internal sealed class UiAcceptanceArmdClient : IArmdClient
{
    private static readonly (double Minimum, double Maximum)[] JointLimits =
    [
        (-2.4, 2.4),
        (-0.1, 3.2),
        (-0.1, 4.0),
        (-1.6, 1.6),
        (-1.7, 1.7),
        (-2.5, 2.5),
    ];

    private bool _held;
    private bool _estopEngaged;
    private bool _teachActive;
    private bool _recording;
    private string _recordingPath = string.Empty;
    private readonly List<TeachRecordingSnapshot> _recordings =
    [
        new("/home/winbeau/.local/share/panthera/teach/pick_cube_good_07.jsonl", DateTimeOffset.Now.AddMinutes(-8), 16.0, 3205),
        new("/home/winbeau/.local/share/panthera/teach/calibration_reach_02.jsonl", DateTimeOffset.Now.AddMinutes(-18), 9.1, 1824),
    ];

    public TerminalConnectionState ConnectionState => TerminalConnectionState.Connected;

    public Task<DaemonSnapshot> GetDaemonStatusAsync(CancellationToken cancellationToken = default) =>
        Task.FromResult(new DaemonSnapshot(true, true, 200.0, "ui-acceptance", false));

    public Task<CameraSnapshot> GetCameraStatusAsync(CancellationToken cancellationToken = default) =>
        Task.FromResult(new CameraSnapshot(
            true, true, true, "RealSense D405 Simulator", "SIM-D405-0001", "sim", "sim", "sim", "", 8, 30.0));

    public async IAsyncEnumerable<CameraFrameSnapshot> StreamCameraFramesAsync(
        CameraStreamKind stream,
        double maxRateHz = 15,
        [EnumeratorCancellation]
        CancellationToken cancellationToken = default)
    {
        const int width = 160;
        const int height = 120;
        var delay = TimeSpan.FromSeconds(1.0 / Math.Clamp(maxRateHz, 1.0, 30.0));
        long sequence = 0;
        while (!cancellationToken.IsCancellationRequested)
        {
            sequence++;
            var data = stream == CameraStreamKind.Color
                ? CreateColorFrame(width, height, sequence)
                : CreateDepthFrame(width, height, sequence);
            yield return new CameraFrameSnapshot(
                stream,
                stream == CameraStreamKind.Color ? CameraPixelKind.Rgb8 : CameraPixelKind.Z16,
                sequence,
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() * 1_000_000,
                width,
                height,
                stream == CameraStreamKind.Color ? width * 3 : width * 2,
                stream == CameraStreamKind.Depth ? 0.001 : 0.0,
                data);
            await Task.Delay(delay, cancellationToken);
        }
    }

    public Task<ControlSnapshot> GetControlStatusAsync(CancellationToken cancellationToken = default) =>
        Task.FromResult(CurrentControl());

    public Task<SoftLimitSnapshot> GetSoftLimitsAsync(CancellationToken cancellationToken = default) =>
        Task.FromResult(new SoftLimitSnapshot(
            JointLimits.Select((limit, index) =>
                    new JointLimitSnapshot($"joint{index + 1}", limit.Minimum, limit.Maximum, 1.0, 10.0))
                .ToArray(),
            0.0,
            2.0,
            1.0,
            10.0,
            false));

    public Task<ControlSnapshot> AcquireControlAsync(
        string clientId,
        bool force = false,
        CancellationToken cancellationToken = default)
    {
        _held = true;
        return Task.FromResult(CurrentControl(clientId));
    }

    public Task ReleaseControlAsync(CancellationToken cancellationToken = default)
    {
        _held = false;
        return Task.CompletedTask;
    }

    public Task TriggerEStopAsync(string reason, CancellationToken cancellationToken = default)
    {
        _estopEngaged = true;
        WriteAcceptanceEvent("estop");
        return Task.CompletedTask;
    }

    public Task ClearEStopAsync(CancellationToken cancellationToken = default)
    {
        _estopEngaged = false;
        return Task.CompletedTask;
    }

    public async IAsyncEnumerable<RobotSnapshot> StreamStateAsync(
        double rateHz = 60,
        [EnumeratorCancellation]
        CancellationToken cancellationToken = default)
    {
        var delay = TimeSpan.FromSeconds(1.0 / Math.Clamp(rateHz, 1.0, 100.0));
        while (!cancellationToken.IsCancellationRequested)
        {
            yield return CreateRobotSnapshot();
            await Task.Delay(delay, cancellationToken);
        }
    }

    public Task<JointMoveResult> MoveJAsync(
        IReadOnlyList<double> positions,
        double durationSeconds,
        bool wait,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new JointMoveResult(true, true, Enumerable.Repeat(0.0, 6).ToArray(), ""));

    public async Task JogAsync(
        IAsyncEnumerable<IReadOnlyList<double>> commands,
        CancellationToken cancellationToken = default)
    {
        await foreach (var command in commands.WithCancellation(cancellationToken))
        {
            var activeJoint = command
                .Select((velocity, index) => (velocity, index))
                .FirstOrDefault(item => Math.Abs(item.velocity) > 0);
            if (Math.Abs(activeJoint.velocity) > 0)
            {
                WriteAcceptanceEvent($"jog:{activeJoint.index}:{activeJoint.velocity:F3}");
            }
        }
    }

    public Task<OperationResult> GripperMoveAsync(
        double position,
        double velocity,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new OperationResult(true));

    public Task<ExecutionHandle> MoveLAsync(
        CartesianTarget target,
        double durationSeconds,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new ExecutionHandle("ui-acceptance-execution"));

    public Task<ExecutionHandle> RunJointTrajectoryAsync(
        IReadOnlyList<JointTrajectoryWaypoint> waypoints,
        IReadOnlyList<double> durations,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new ExecutionHandle("ui-acceptance-joint-trajectory"));

    public async IAsyncEnumerable<ExecutionProgress> StreamExecutionAsync(
        string executionId,
        [EnumeratorCancellation]
        CancellationToken cancellationToken = default)
    {
        await Task.Yield();
        yield return new ExecutionProgress(executionId, ExecutionState.Done, 1.0, "", CreateRobotSnapshot());
    }

    public Task<bool> CancelExecutionAsync(
        string executionId,
        CancellationToken cancellationToken = default) => Task.FromResult(true);

    public Task<IReadOnlyList<double>> ForwardKinematicsAsync(
        IReadOnlyList<double> joints,
        CancellationToken cancellationToken = default) =>
        Task.FromResult<IReadOnlyList<double>>([0.25, 0.0, 0.17, 0.0, 0.0, 0.0]);

    public Task<OperationResult> StartTeachAsync(CancellationToken cancellationToken = default)
    {
        _teachActive = true;
        return Task.FromResult(new OperationResult(true));
    }

    public Task<OperationResult> StopTeachAsync(CancellationToken cancellationToken = default)
    {
        _teachActive = false;
        _recording = false;
        return Task.FromResult(new OperationResult(true));
    }

    public Task<string> StartTeachRecordingAsync(
        string path = "",
        CancellationToken cancellationToken = default)
    {
        if (!_teachActive)
        {
            throw new InvalidOperationException("请先启动拖动示教");
        }
        _recording = true;
        _recordingPath = string.IsNullOrWhiteSpace(path)
            ? "/home/winbeau/.local/share/panthera/teach/trajectory_ui_acceptance.jsonl"
            : $"/home/winbeau/.local/share/panthera/teach/{path}";
        return Task.FromResult(_recordingPath);
    }

    public Task<TeachRecordingSnapshot?> StopTeachRecordingAsync(
        CancellationToken cancellationToken = default)
    {
        if (!_recording)
        {
            return Task.FromResult<TeachRecordingSnapshot?>(null);
        }
        _recording = false;
        var result = new TeachRecordingSnapshot(
            _recordingPath,
            DateTimeOffset.Now,
            12.4,
            2486);
        _recordings.Insert(0, result);
        return Task.FromResult<TeachRecordingSnapshot?>(result);
    }

    public Task<IReadOnlyList<TeachRecordingSnapshot>> ListTeachRecordingsAsync(
        CancellationToken cancellationToken = default) =>
        Task.FromResult<IReadOnlyList<TeachRecordingSnapshot>>(_recordings.ToArray());

    public Task<ExecutionHandle> PlayTeachRecordingAsync(
        string path,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new ExecutionHandle("ui-acceptance-teach-playback"));

    public Task<DatasetJobHandle> ExportLeRobotAsync(
        string trajectoryPath,
        string outputDirectory,
        string repoId,
        string task,
        bool overwrite = false,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new DatasetJobHandle("ui-acceptance-dataset"));

    public async IAsyncEnumerable<DatasetJobSnapshot> WatchDatasetJobAsync(
        string jobId,
        [EnumeratorCancellation]
        CancellationToken cancellationToken = default)
    {
        yield return new DatasetJobSnapshot(jobId, DatasetExportState.Running, 0.45, "", 0, "");
        await Task.Delay(80, cancellationToken);
        yield return new DatasetJobSnapshot(jobId, DatasetExportState.Done, 1.0, "datasets/ui-acceptance", 2486, "");
    }

    public Task<bool> CancelDatasetJobAsync(
        string jobId,
        CancellationToken cancellationToken = default) => Task.FromResult(true);

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    private static void WriteAcceptanceEvent(string message)
    {
        var path = Environment.GetEnvironmentVariable("PANTHERA_UI_ACCEPTANCE_LOG");
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }

        Directory.CreateDirectory(Path.GetDirectoryName(path) ?? ".");
        File.AppendAllText(path, $"{message}{Environment.NewLine}");
    }

    private ControlSnapshot CurrentControl(string? holder = null) =>
        new(_held, _held ? holder ?? Environment.MachineName : "", true, _estopEngaged);

    private RobotSnapshot CreateRobotSnapshot()
    {
        var joints = Enumerable.Range(1, 6)
            .Select(index => new MotorSnapshot($"joint{index}", index, 0.0, 0.0, 0.0, 21, 0, 0, 0, true))
            .ToArray();
        var gripper = new MotorSnapshot("joint7", 7, 0.8, 0.0, 0.0, 21, 0, 0, 0, true);
        return new RobotSnapshot(joints, gripper, 0, _estopEngaged, DateTimeOffset.UtcNow);
    }

    private static byte[] CreateColorFrame(int width, int height, long sequence)
    {
        var data = new byte[width * height * 3];
        for (var y = 0; y < height; y++)
        {
            for (var x = 0; x < width; x++)
            {
                var offset = (y * width + x) * 3;
                data[offset] = (byte)((x + sequence) % 256);
                data[offset + 1] = (byte)((y * 2 + sequence) % 256);
                data[offset + 2] = (byte)((x + y) % 256);
            }
        }
        return data;
    }

    private static byte[] CreateDepthFrame(int width, int height, long sequence)
    {
        var data = new byte[width * height * 2];
        for (var index = 0; index < width * height; index++)
        {
            var depth = (ushort)(250 + (index % width) * 4 + sequence % 80);
            data[index * 2] = (byte)(depth & 0xff);
            data[index * 2 + 1] = (byte)(depth >> 8);
        }
        return data;
    }
}
