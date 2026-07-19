using System.Runtime.CompilerServices;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

internal sealed class UiAcceptanceArmdClient : IArmdClient
{
    private bool _held;
    private bool _estopEngaged;

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
            Enumerable.Range(1, 6)
                .Select(index => new JointLimitSnapshot($"joint{index}", -3.14, 3.14, 1.0, 10.0))
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
        await foreach (var _ in commands.WithCancellation(cancellationToken))
        {
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

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

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
