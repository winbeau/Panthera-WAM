using System.Text.Json;
using Panthera.Terminal.Grpc;

var endpoint = args.FirstOrDefault(argument => !argument.StartsWith("--", StringComparison.Ordinal))
    ?? "http://127.0.0.1:50051";
var statusOnly = args.Contains("--status-only", StringComparer.Ordinal);
var stressJog = args.Contains("--stress-jog", StringComparer.Ordinal);
var stateOnly = args.Contains("--state-only", StringComparer.Ordinal);
var heartbeatOnly = args.Contains("--heartbeat-only", StringComparer.Ordinal);
var jogOnly = args.Contains("--jog-only", StringComparer.Ordinal);
var allowHardwareJog = args.Contains("--allow-hardware-jog", StringComparer.Ordinal);
var returnJog = args.Contains("--return-jog", StringComparer.Ordinal);
var stressSeconds = args
    .Where(argument => argument.StartsWith("--stress-seconds=", StringComparison.Ordinal))
    .Select(argument => double.Parse(argument["--stress-seconds=".Length..]))
    .DefaultIfEmpty(5.0)
    .Single();
var jogVelocity = args
    .Where(argument => argument.StartsWith("--jog-velocity=", StringComparison.Ordinal))
    .Select(argument => double.Parse(argument["--jog-velocity=".Length..]))
    .DefaultIfEmpty(0.0)
    .Single();
await using var client = new ArmdClient(endpoint);
var daemon = await client.GetDaemonStatusAsync();
var limits = await client.GetSoftLimitsAsync();
if (statusOnly)
{
    var status = await client.GetControlStatusAsync();
    Panthera.Terminal.Core.RobotSnapshot? robot = null;
    using var statusStateLifetime = new CancellationTokenSource(TimeSpan.FromSeconds(3));
    try
    {
        await foreach (var snapshot in client.StreamStateAsync(20, statusStateLifetime.Token))
        {
            robot = snapshot;
            break;
        }
    }
    catch (OperationCanceledException)
    {
    }
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        endpoint,
        daemon.Simulation,
        daemon.HardwareConnected,
        JointCount = limits.Joints.Count,
        status.Held,
        status.HolderClientId,
        status.WatchdogOk,
        client.ConnectionState,
        JointModes = robot?.Joints.Select(joint => joint.Mode).ToArray(),
        JointPositions = robot?.Joints.Select(joint => joint.Position).ToArray(),
        JointValid = robot?.Joints.All(joint => joint.Valid),
        GripperMode = robot?.Gripper.Mode,
        GripperValid = robot?.Gripper.Valid,
    }));
    return;
}
if (stateOnly)
{
    using var stateOnlyLifetime = new CancellationTokenSource(TimeSpan.FromSeconds(stressSeconds));
    var samples = 0;
    try
    {
        await foreach (var _ in client.StreamStateAsync(60, stateOnlyLifetime.Token))
        {
            samples++;
        }
    }
    catch (OperationCanceledException)
    {
    }
    Console.WriteLine(JsonSerializer.Serialize(new { endpoint, StateSamples = samples }));
    return;
}
if (stressJog && !daemon.Simulation && !allowHardwareJog)
{
    throw new InvalidOperationException("--stress-jog 仅允许连接仿真服务");
}
if (Math.Abs(jogVelocity) > 0.1)
{
    throw new InvalidOperationException("诊断点动速度不得超过 0.1 rad/s");
}
var acquired = await client.AcquireControlAsync($"wpf-smoke@{Environment.MachineName}");
var heartbeatSamples = new List<object>();
var stateSamples = 0;
var stateFailure = string.Empty;
CancellationTokenSource? stateLifetime = null;
Task? stateTask = null;
if (stressJog && !jogOnly)
{
    stateLifetime = new CancellationTokenSource();
    stateTask = Task.Run(async () =>
    {
        try
        {
            await foreach (var _ in client.StreamStateAsync(60, stateLifetime.Token))
            {
                Interlocked.Increment(ref stateSamples);
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception exception)
        {
            stateFailure = exception.Message;
        }
    });
    await client.JogAsync(JogCommands(stressSeconds, jogVelocity));
    if (returnJog && jogVelocity != 0)
    {
        await client.JogAsync(JogCommands(stressSeconds, -jogVelocity));
    }
}
else if (stressJog)
{
    await client.JogAsync(JogCommands(stressSeconds, jogVelocity));
    if (returnJog && jogVelocity != 0)
    {
        await client.JogAsync(JogCommands(stressSeconds, -jogVelocity));
    }
}
var heartbeatSampleCount = heartbeatOnly
    ? Math.Max(1, (int)Math.Ceiling(stressSeconds / 0.4))
    : 5;
for (var sample = 0; sample < heartbeatSampleCount; sample++)
{
    await Task.Delay(400);
    var status = await client.GetControlStatusAsync();
    heartbeatSamples.Add(new { status.Held, status.HolderClientId, status.WatchdogOk });
}
var control = await client.GetControlStatusAsync();
if (stateLifetime is not null && stateTask is not null)
{
    stateLifetime.Cancel();
    await stateTask;
    stateLifetime.Dispose();
}
var released = true;
try
{
    await client.ReleaseControlAsync();
}
catch (Exception exception)
{
    released = false;
    heartbeatSamples.Add(new { Error = exception.Message });
}
Console.WriteLine(JsonSerializer.Serialize(new
{
    endpoint,
    daemon.HardwareConnected,
    JointCount = limits.Joints.Count,
    Acquired = acquired.Held,
    HeartbeatHeld = control.Held,
    Released = released,
    StateSamples = stateSamples,
    StateFailure = stateFailure,
    HeartbeatSamples = heartbeatSamples,
}));

static async IAsyncEnumerable<IReadOnlyList<double>> JogCommands(double seconds, double velocity)
{
    var commandCount = Math.Max(1, (int)Math.Ceiling(seconds / 0.05));
    for (var index = 0; index < commandCount; index++)
    {
        yield return new[] { velocity, 0.0, 0.0, 0.0, 0.0, 0.0 };
        await Task.Delay(50);
    }
}
