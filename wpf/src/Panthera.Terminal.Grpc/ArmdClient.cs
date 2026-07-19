using System.Runtime.CompilerServices;
using Grpc.Core;
using Grpc.Net.Client;
using Panthera.Arm.V1;
using Panthera.Camera.V1;
using Panthera.Dataset.V1;
using Panthera.Terminal.Core;
using DatasetProtoState = Panthera.Dataset.V1.DatasetJobState;

namespace Panthera.Terminal.Grpc;

public sealed class ArmdClient : IArmdClient
{
    private readonly GrpcChannel _channel;
    private readonly GrpcChannel _stateChannel;
    private readonly GrpcChannel _heartbeatChannel;
    private readonly GrpcChannel _jogChannel;
    private readonly GrpcChannel _executionChannel;
    private readonly GrpcChannel _cameraChannel;
    private readonly ArmService.ArmServiceClient _client;
    private readonly ArmService.ArmServiceClient _stateClient;
    private readonly ArmService.ArmServiceClient _heartbeatClient;
    private readonly ArmService.ArmServiceClient _jogClient;
    private readonly ArmService.ArmServiceClient _executionClient;
    private readonly CameraService.CameraServiceClient _cameraClient;
    private readonly DatasetService.DatasetServiceClient _datasetClient;
    private readonly CancellationTokenSource _lifetime = new();
    private CancellationTokenSource? _heartbeatLifetime;
    private Task? _heartbeatTask;
    private string _leaseToken = string.Empty;

    public ArmdClient(string endpoint, string? cameraEndpoint = null)
    {
        _channel = CreateChannel(endpoint);
        _stateChannel = CreateChannel(endpoint);
        _heartbeatChannel = CreateChannel(endpoint);
        _jogChannel = CreateChannel(endpoint);
        _executionChannel = CreateChannel(endpoint);
        _cameraChannel = CreateChannel(cameraEndpoint ?? endpoint);
        _client = new ArmService.ArmServiceClient(_channel);
        _stateClient = new ArmService.ArmServiceClient(_stateChannel);
        _heartbeatClient = new ArmService.ArmServiceClient(_heartbeatChannel);
        _jogClient = new ArmService.ArmServiceClient(_jogChannel);
        _executionClient = new ArmService.ArmServiceClient(_executionChannel);
        _cameraClient = new CameraService.CameraServiceClient(_cameraChannel);
        _datasetClient = new DatasetService.DatasetServiceClient(_executionChannel);
    }

    public TerminalConnectionState ConnectionState { get; private set; } = TerminalConnectionState.Disconnected;

    public async Task<DaemonSnapshot> GetDaemonStatusAsync(CancellationToken cancellationToken = default)
    {
        return await InvokeRetryableAsync(async () =>
        {
            var response = await _client.GetDaemonStatusAsync(
                new Empty(),
                deadline: DateTime.UtcNow.AddSeconds(3),
                cancellationToken: cancellationToken);
            return new DaemonSnapshot(
                response.Sim,
                response.HardwareConnected,
                response.ControlHz,
                response.SdkVersion,
                response.EstopLatchHazardPresent);
        }, cancellationToken);
    }

    public async Task<CameraSnapshot> GetCameraStatusAsync(CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _cameraClient.GetStatusAsync(
            new CameraStatusRequest(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken), cancellationToken);
        return new CameraSnapshot(
            response.Enabled,
            response.Available,
            response.Streaming,
            response.Model,
            response.Serial,
            response.Firmware,
            response.UsbType,
            response.SdkVersion,
            response.Error,
            response.LastFrameAgeMs,
            response.ActualFps);
    }

    public async IAsyncEnumerable<CameraFrameSnapshot> StreamCameraFramesAsync(
        CameraStreamKind stream,
        double maxRateHz = 15,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        if (maxRateHz <= 0 || maxRateHz > 90)
        {
            throw new ArgumentOutOfRangeException(nameof(maxRateHz));
        }
        var request = new StreamFramesRequest
        {
            Stream = stream == CameraStreamKind.Depth
                ? CameraStreamType.Depth
                : CameraStreamType.Color,
            MaxRateHz = maxRateHz,
        };
        using var call = _cameraClient.StreamFrames(request, cancellationToken: cancellationToken);
        while (await call.ResponseStream.MoveNext(cancellationToken))
        {
            var frame = call.ResponseStream.Current;
            yield return new CameraFrameSnapshot(
                frame.Stream == CameraStreamType.Depth
                    ? CameraStreamKind.Depth
                    : CameraStreamKind.Color,
                frame.PixelFormat == CameraPixelFormat.Z16
                    ? CameraPixelKind.Z16
                    : CameraPixelKind.Rgb8,
                frame.Sequence,
                frame.CapturedAtNs,
                frame.Width,
                frame.Height,
                frame.Stride,
                frame.DepthScale,
                frame.Data.ToByteArray());
        }
    }

    public async Task<ControlSnapshot> GetControlStatusAsync(CancellationToken cancellationToken = default)
    {
        return await InvokeRetryableAsync(async () => MapControl(await _client.GetControlStatusAsync(
            new Empty(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken)), cancellationToken);
    }

    public async Task<SoftLimitSnapshot> GetSoftLimitsAsync(CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _client.GetSoftLimitsAsync(
            new Empty(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken), cancellationToken);
        return new SoftLimitSnapshot(
            response.JointLimits.Select(limit => new JointLimitSnapshot(
                limit.Name,
                limit.PosMin,
                limit.PosMax,
                limit.VelMax,
                limit.TorqueMax)).ToArray(),
            response.GripperLimit.PosMin,
            response.GripperLimit.PosMax,
            response.GripperLimit.VelMax,
            response.GripperLimit.TorqueMax,
            response.HardwareLimitsEnabled);
    }

    public async Task<ControlSnapshot> AcquireControlAsync(
        string clientId,
        bool force = false,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _client.AcquireControlAsync(
            new AcquireControlRequest { ClientId = clientId, Force = force },
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken), cancellationToken);
        if (response.Granted)
        {
            _leaseToken = response.LeaseToken;
            StartHeartbeat();
        }
        return new ControlSnapshot(
            response.Granted,
            response.HolderClientId,
            response.Granted,
            false);
    }

    public async Task ReleaseControlAsync(CancellationToken cancellationToken = default)
    {
        await InvokeAsync(async () =>
        {
            await _client.ReleaseControlAsync(
                new Empty(),
                Headers(),
                deadline: DateTime.UtcNow.AddSeconds(3),
                cancellationToken: cancellationToken);
            return true;
        });
        StopHeartbeat();
        _leaseToken = string.Empty;
    }

    public async Task TriggerEStopAsync(string reason, CancellationToken cancellationToken = default)
    {
        await InvokeAsync(async () =>
        {
            await _client.EStopAsync(
                new EStopRequest { Reason = reason },
                deadline: DateTime.UtcNow.AddSeconds(3),
                cancellationToken: cancellationToken);
            return true;
        });
    }

    public async Task ClearEStopAsync(CancellationToken cancellationToken = default)
    {
        await InvokeAsync(async () =>
        {
            await _client.ClearEStopAsync(
                new ClearEStopRequest { Confirm = true },
                Headers(),
                deadline: DateTime.UtcNow.AddSeconds(3),
                cancellationToken: cancellationToken);
            return true;
        });
    }

    public async IAsyncEnumerable<RobotSnapshot> StreamStateAsync(
        double rateHz = 60,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        if (rateHz <= 0 || rateHz > 100)
        {
            throw new ArgumentOutOfRangeException(nameof(rateHz));
        }
        var period = TimeSpan.FromSeconds(1.0 / rateHz);
        while (!cancellationToken.IsCancellationRequested)
        {
            var startedAt = DateTime.UtcNow;
            RobotState state;
            try
            {
                state = await _stateClient.GetRobotStateAsync(
                    new Empty(),
                    deadline: DateTime.UtcNow.AddMilliseconds(500),
                    cancellationToken: cancellationToken);
                ConnectionState = TerminalConnectionState.Connected;
            }
            catch (RpcException exception) when (
                exception.StatusCode is StatusCode.Unavailable or StatusCode.DeadlineExceeded)
            {
                ConnectionState = TerminalConnectionState.Disconnected;
                await Task.Delay(TimeSpan.FromMilliseconds(50), cancellationToken);
                continue;
            }
            yield return MapRobot(state);
            var remaining = period - (DateTime.UtcNow - startedAt);
            if (remaining > TimeSpan.Zero)
            {
                await Task.Delay(remaining, cancellationToken);
            }
        }
    }

    public async Task<JointMoveResult> MoveJAsync(
        IReadOnlyList<double> positions,
        double durationSeconds,
        bool wait,
        CancellationToken cancellationToken = default)
    {
        var request = new MoveJRequest { DurationS = durationSeconds, Wait = wait };
        request.Positions.AddRange(positions);
        var response = await InvokeAsync(async () => await _client.MoveJAsync(
            request,
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(durationSeconds + 5),
            cancellationToken: cancellationToken));
        return new JointMoveResult(
            response.Accepted,
            response.Reached,
            response.Errors.ToArray(),
            response.RejectReason);
    }

    public async Task JogAsync(
        IAsyncEnumerable<IReadOnlyList<double>> commands,
        CancellationToken cancellationToken = default)
    {
        try
        {
            await foreach (var values in commands.WithCancellation(cancellationToken))
            {
                var command = new JointJogCommand();
                command.Velocities.AddRange(values);
                await SendJogStepWithRetryAsync(command, cancellationToken);
            }
        }
        catch (RpcException exception)
        {
            ConnectionState = exception.StatusCode == StatusCode.Unavailable
                ? TerminalConnectionState.Disconnected
                : TerminalConnectionState.Faulted;
            throw new ArmdClientException(exception.StatusCode, exception.Status.Detail, exception);
        }
        finally
        {
            try
            {
                await _jogClient.StopJointJogAsync(
                    new Empty(),
                    Headers(),
                    deadline: DateTime.UtcNow.AddSeconds(1),
                    cancellationToken: CancellationToken.None);
            }
            catch (Exception)
            {
            }
        }
    }

    public async Task<OperationResult> GripperMoveAsync(
        double position,
        double velocity,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.GripperMoveAsync(
            new GripperMoveRequest { Position = position, Velocity = velocity, MaxTorque = 0.5 },
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken));
        return new OperationResult(response.Accepted, response.RejectReason);
    }

    public async Task<ExecutionHandle> MoveLAsync(
        CartesianTarget target,
        double durationSeconds,
        CancellationToken cancellationToken = default)
    {
        var pose = new CartesianPose { Position = { target.X, target.Y, target.Z } };
        if (!target.PreserveOrientation)
        {
            pose.Rpy = new RPY { Roll = target.Roll, Pitch = target.Pitch, Yaw = target.Yaw };
        }
        var response = await InvokeAsync(async () => await _client.MoveLAsync(
            new MoveLRequest
            {
                Target = pose,
                DurationS = durationSeconds,
                UseSpline = true,
            },
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(15),
            cancellationToken: cancellationToken));
        return new ExecutionHandle(response.ExecutionId);
    }

    public async IAsyncEnumerable<ExecutionProgress> StreamExecutionAsync(
        string executionId,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var call = _executionClient.StreamExecution(
            new StreamExecutionRequest { ExecutionId = executionId },
            cancellationToken: cancellationToken);
        while (await call.ResponseStream.MoveNext(cancellationToken))
        {
            var value = call.ResponseStream.Current;
            yield return new ExecutionProgress(
                value.ExecutionId,
                value.State switch
                {
                    ExecState.Running => ExecutionState.Running,
                    ExecState.Done => ExecutionState.Done,
                    ExecState.Cancelled => ExecutionState.Cancelled,
                    _ => ExecutionState.Failed,
                },
                value.Fraction,
                value.ErrorMessage,
                value.RobotState is not null ? MapRobot(value.RobotState) : null);
        }
    }

    public async Task<bool> CancelExecutionAsync(
        string executionId,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.CancelExecutionAsync(
            new CancelExecutionRequest { ExecutionId = executionId },
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken));
        return response.Cancelled;
    }

    public async Task<IReadOnlyList<double>> ForwardKinematicsAsync(
        IReadOnlyList<double> joints,
        CancellationToken cancellationToken = default)
    {
        var request = new JointAnglesOptional();
        request.JointAngles.AddRange(joints);
        var response = await InvokeRetryableAsync(async () => await _client.GetForwardKinematicsAsync(
            request,
            deadline: DateTime.UtcNow.AddSeconds(5),
            cancellationToken: cancellationToken), cancellationToken);
        return response.Position.ToArray();
    }

    public async Task<OperationResult> StartTeachAsync(CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.TeachStartAsync(
            new TeachStartRequest(),
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken));
        return new OperationResult(response.Accepted, response.RejectReason);
    }

    public async Task<OperationResult> StopTeachAsync(CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.TeachStopAsync(
            new Empty(),
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken));
        return new OperationResult(response.Accepted, response.Accepted ? string.Empty : "拖动示教未启动");
    }

    public async Task<string> StartTeachRecordingAsync(
        string path = "",
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.TeachRecordStartAsync(
            new TeachRecordStartRequest { Path = path ?? string.Empty },
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken));
        if (!response.Accepted)
        {
            throw new InvalidOperationException("示教录制已在运行");
        }
        return response.Path;
    }

    public async Task<TeachRecordingSnapshot?> StopTeachRecordingAsync(
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.TeachRecordStopAsync(
            new Empty(),
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(8),
            cancellationToken: cancellationToken));
        return response.Accepted
            ? new TeachRecordingSnapshot(
                response.SavedPath,
                DateTimeOffset.Now,
                0,
                response.FrameCount)
            : null;
    }

    public async Task<IReadOnlyList<TeachRecordingSnapshot>> ListTeachRecordingsAsync(
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _client.TeachListAsync(
            new Empty(),
            deadline: DateTime.UtcNow.AddSeconds(5),
            cancellationToken: cancellationToken), cancellationToken);
        return response.Files.Select(file => new TeachRecordingSnapshot(
            file.Path,
            file.RecordedAt > 0
                ? DateTimeOffset.FromUnixTimeMilliseconds(file.RecordedAt)
                : DateTimeOffset.MinValue,
            file.DurationS,
            file.FrameCount)).ToArray();
    }

    public async Task<ExecutionHandle> PlayTeachRecordingAsync(
        string path,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeAsync(async () => await _client.TeachPlayAsync(
            new TeachPlayRequest
            {
                Path = path,
                Mode = PlaybackMode.Posvel,
            },
            Headers(),
            deadline: DateTime.UtcNow.AddSeconds(15),
            cancellationToken: cancellationToken));
        return new ExecutionHandle(response.ExecutionId);
    }

    public async Task<DatasetJobHandle> ExportLeRobotAsync(
        string trajectoryPath,
        string outputDirectory,
        string repoId,
        string task,
        bool overwrite = false,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _datasetClient.ExportLeRobotAsync(
            new ExportLeRobotRequest
            {
                TrajectoryPath = trajectoryPath,
                OutputDir = outputDirectory,
                RepoId = repoId,
                Task = task,
                Overwrite = overwrite,
            },
            deadline: DateTime.UtcNow.AddSeconds(8),
            cancellationToken: cancellationToken), cancellationToken);
        return new DatasetJobHandle(response.JobId);
    }

    public async IAsyncEnumerable<DatasetJobSnapshot> WatchDatasetJobAsync(
        string jobId,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var call = _datasetClient.WatchJob(
            new DatasetJobRequest { JobId = jobId },
            cancellationToken: cancellationToken);
        while (await call.ResponseStream.MoveNext(cancellationToken))
        {
            var value = call.ResponseStream.Current;
            yield return new DatasetJobSnapshot(
                value.JobId,
                value.State switch
                {
                    DatasetProtoState.Queued => DatasetExportState.Queued,
                    DatasetProtoState.Running => DatasetExportState.Running,
                    DatasetProtoState.Done => DatasetExportState.Done,
                    DatasetProtoState.Cancelled => DatasetExportState.Cancelled,
                    _ => DatasetExportState.Failed,
                },
                value.Progress,
                value.OutputDir,
                value.FrameCount,
                value.ErrorMessage);
        }
    }

    public async Task<bool> CancelDatasetJobAsync(
        string jobId,
        CancellationToken cancellationToken = default)
    {
        var response = await InvokeRetryableAsync(async () => await _datasetClient.CancelJobAsync(
            new DatasetJobRequest { JobId = jobId },
            deadline: DateTime.UtcNow.AddSeconds(3),
            cancellationToken: cancellationToken), cancellationToken);
        return response.Cancelled;
    }

    public async ValueTask DisposeAsync()
    {
        _lifetime.Cancel();
        StopHeartbeat();
        if (_heartbeatTask is not null)
        {
            try
            {
                await _heartbeatTask.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
        }
        _channel.Dispose();
        _stateChannel.Dispose();
        _heartbeatChannel.Dispose();
        _jogChannel.Dispose();
        _executionChannel.Dispose();
        _cameraChannel.Dispose();
        _lifetime.Dispose();
    }

    private Metadata Headers()
    {
        if (string.IsNullOrWhiteSpace(_leaseToken))
        {
            throw new InvalidOperationException("尚未获取控制权");
        }
        return new Metadata { { "x-panthera-lease", _leaseToken } };
    }

    private void StartHeartbeat()
    {
        StopHeartbeat();
        _heartbeatLifetime = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        _heartbeatTask = HeartbeatLoopAsync(_heartbeatLifetime.Token);
    }

    private void StopHeartbeat()
    {
        _heartbeatLifetime?.Cancel();
        _heartbeatLifetime?.Dispose();
        _heartbeatLifetime = null;
    }

    private async Task HeartbeatLoopAsync(CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await _heartbeatClient.HeartbeatOnceAsync(
                        new HeartbeatRequest(),
                        Headers(),
                        deadline: DateTime.UtcNow.AddSeconds(1),
                        cancellationToken: cancellationToken);
                    ConnectionState = TerminalConnectionState.Connected;
                    await Task.Delay(TimeSpan.FromMilliseconds(500), cancellationToken);
                }
                catch (RpcException exception) when (
                    exception.StatusCode is StatusCode.Unavailable or StatusCode.DeadlineExceeded)
                {
                    ConnectionState = TerminalConnectionState.Disconnected;
                    await Task.Delay(TimeSpan.FromMilliseconds(100), cancellationToken);
                }
                catch (RpcException)
                {
                    ConnectionState = TerminalConnectionState.Faulted;
                    return;
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private async Task<T> InvokeAsync<T>(Func<Task<T>> action)
    {
        try
        {
            ConnectionState = TerminalConnectionState.Connecting;
            var result = await action();
            ConnectionState = TerminalConnectionState.Connected;
            return result;
        }
        catch (RpcException exception)
        {
            ConnectionState = exception.StatusCode == StatusCode.Unavailable
                ? TerminalConnectionState.Disconnected
                : TerminalConnectionState.Faulted;
            throw new ArmdClientException(exception.StatusCode, exception.Status.Detail, exception);
        }
    }

    private async Task<T> InvokeRetryableAsync<T>(
        Func<Task<T>> action,
        CancellationToken cancellationToken)
    {
        var retryUntil = DateTime.UtcNow.AddSeconds(5);
        while (true)
        {
            try
            {
                ConnectionState = TerminalConnectionState.Connecting;
                var result = await action();
                ConnectionState = TerminalConnectionState.Connected;
                return result;
            }
            catch (RpcException exception) when (
                (exception.StatusCode is StatusCode.Unavailable or StatusCode.DeadlineExceeded)
                && DateTime.UtcNow < retryUntil)
            {
                ConnectionState = TerminalConnectionState.Disconnected;
                await Task.Delay(TimeSpan.FromMilliseconds(100), cancellationToken);
            }
            catch (RpcException exception)
            {
                ConnectionState = exception.StatusCode == StatusCode.Unavailable
                    ? TerminalConnectionState.Disconnected
                    : TerminalConnectionState.Faulted;
                throw new ArmdClientException(exception.StatusCode, exception.Status.Detail, exception);
            }
        }
    }

    private async Task SendJogStepWithRetryAsync(
        JointJogCommand command,
        CancellationToken cancellationToken)
    {
        var retryUntil = DateTime.UtcNow.AddSeconds(1.5);
        while (true)
        {
            try
            {
                await _jogClient.JointJogStepAsync(
                    command,
                    Headers(),
                    deadline: DateTime.UtcNow.AddMilliseconds(400),
                    cancellationToken: cancellationToken);
                ConnectionState = TerminalConnectionState.Connected;
                return;
            }
            catch (RpcException exception) when (
                (exception.StatusCode is StatusCode.Unavailable or StatusCode.DeadlineExceeded)
                && DateTime.UtcNow < retryUntil)
            {
                ConnectionState = TerminalConnectionState.Disconnected;
                await Task.Delay(TimeSpan.FromMilliseconds(20), cancellationToken);
            }
            catch (RpcException exception)
            {
                ConnectionState = exception.StatusCode == StatusCode.Unavailable
                    ? TerminalConnectionState.Disconnected
                    : TerminalConnectionState.Faulted;
                throw new ArmdClientException(exception.StatusCode, exception.Status.Detail, exception);
            }
        }
    }

    private static GrpcChannel CreateChannel(string endpoint)
    {
        var handler = new SocketsHttpHandler
        {
            UseProxy = false,
            EnableMultipleHttp2Connections = false,
            ConnectTimeout = TimeSpan.FromSeconds(3),
        };
        return GrpcChannel.ForAddress(endpoint, new GrpcChannelOptions { HttpHandler = handler });
    }

    private static ControlSnapshot MapControl(ControlStatus value) =>
        new(value.Held, value.HolderClientId, value.WatchdogOk, value.EstopEngaged);

    private static RobotSnapshot MapRobot(RobotState value)
    {
        var joints = value.Joint.Joints.Select(MapMotor).ToArray();
        return new RobotSnapshot(
            joints,
            MapMotor(value.Gripper.State),
            value.AgeMs,
            value.EstopEngaged,
            DateTimeOffset.UtcNow);
    }

    private static MotorSnapshot MapMotor(MotorState value) =>
        new(
            value.Name,
            value.MotorId,
            value.Position,
            value.Velocity,
            value.Torque,
            value.Mode,
            value.Fault,
            value.PosLimitFlag,
            value.TorLimitFlag,
            value.Valid);
}

public sealed class ArmdClientException : Exception
{
    public ArmdClientException(StatusCode statusCode, string message, Exception innerException)
        : base(message, innerException)
    {
        StatusCode = statusCode;
    }

    public StatusCode StatusCode { get; }
}
