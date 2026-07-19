namespace Panthera.Terminal.Core;

public enum TerminalConnectionState
{
    Disconnected,
    Connecting,
    Connected,
    Faulted,
}

public enum ExecutionState
{
    Running,
    Done,
    Failed,
    Cancelled,
}

public sealed record MotorSnapshot(
    string Name,
    int MotorId,
    double Position,
    double Velocity,
    double Torque,
    uint Mode,
    uint Fault,
    int PositionLimitFlag,
    int TorqueLimitFlag,
    bool Valid);

public sealed record RobotSnapshot(
    IReadOnlyList<MotorSnapshot> Joints,
    MotorSnapshot Gripper,
    long AgeMs,
    bool EStopEngaged,
    DateTimeOffset ReceivedAt);

public sealed record DaemonSnapshot(
    bool Simulation,
    bool HardwareConnected,
    double ControlHz,
    string SdkVersion,
    bool EStopLatchHazardPresent);

public sealed record CameraSnapshot(
    bool Enabled,
    bool Available,
    bool Streaming,
    string Model,
    string Serial,
    string Firmware,
    string UsbType,
    string SdkVersion,
    string Error,
    long LastFrameAgeMs,
    double ActualFps);

public sealed record ControlSnapshot(
    bool Held,
    string HolderClientId,
    bool WatchdogOk,
    bool EStopEngaged);

public sealed record JointLimitSnapshot(
    string Name,
    double Minimum,
    double Maximum,
    double MaximumVelocity,
    double MaximumTorque);

public sealed record SoftLimitSnapshot(
    IReadOnlyList<JointLimitSnapshot> Joints,
    double GripperMinimum,
    double GripperMaximum,
    double GripperMaximumVelocity,
    double GripperMaximumTorque,
    bool HardwareLimitsEnabled);

public sealed record OperationResult(bool Accepted, string RejectReason = "");

public sealed record JointMoveResult(
    bool Accepted,
    bool Reached,
    IReadOnlyList<double> Errors,
    string RejectReason);

public sealed record ExecutionHandle(string ExecutionId);

public sealed record ExecutionProgress(
    string ExecutionId,
    ExecutionState State,
    double Fraction,
    string ErrorMessage,
    RobotSnapshot? RobotState);

public sealed record CartesianTarget(
    double X,
    double Y,
    double Z,
    double Roll,
    double Pitch,
    double Yaw,
    bool PreserveOrientation = true);

public sealed record TerminalSettings(
    string Endpoint = "http://127.0.0.1:50050",
    string Theme = "System",
    string WslDistribution = "Ubuntu-22.04",
    string WslUser = "",
    string UsbSerial = "",
    double JogSpeed = 0.15,
    double JogStep = 0.02);

public sealed record TerminalLogEntry(
    DateTimeOffset Timestamp,
    string Level,
    string Source,
    string Message);
