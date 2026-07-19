from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXEC_STATE_UNSPECIFIED: _ClassVar[ExecState]
    EXEC_STATE_RUNNING: _ClassVar[ExecState]
    EXEC_STATE_DONE: _ClassVar[ExecState]
    EXEC_STATE_FAILED: _ClassVar[ExecState]
    EXEC_STATE_CANCELLED: _ClassVar[ExecState]

class DynamicsTerm(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DYNAMICS_TERM_UNSPECIFIED: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_GRAVITY: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_CORIOLIS: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_MASS_MATRIX: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_INERTIA: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_FULL_INVERSE_DYNAMICS: _ClassVar[DynamicsTerm]
    DYNAMICS_TERM_FRICTION: _ClassVar[DynamicsTerm]

class PlaybackMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLAYBACK_MODE_UNSPECIFIED: _ClassVar[PlaybackMode]
    PLAYBACK_MODE_MIT: _ClassVar[PlaybackMode]
    PLAYBACK_MODE_POSVEL: _ClassVar[PlaybackMode]
EXEC_STATE_UNSPECIFIED: ExecState
EXEC_STATE_RUNNING: ExecState
EXEC_STATE_DONE: ExecState
EXEC_STATE_FAILED: ExecState
EXEC_STATE_CANCELLED: ExecState
DYNAMICS_TERM_UNSPECIFIED: DynamicsTerm
DYNAMICS_TERM_GRAVITY: DynamicsTerm
DYNAMICS_TERM_CORIOLIS: DynamicsTerm
DYNAMICS_TERM_MASS_MATRIX: DynamicsTerm
DYNAMICS_TERM_INERTIA: DynamicsTerm
DYNAMICS_TERM_FULL_INVERSE_DYNAMICS: DynamicsTerm
DYNAMICS_TERM_FRICTION: DynamicsTerm
PLAYBACK_MODE_UNSPECIFIED: PlaybackMode
PLAYBACK_MODE_MIT: PlaybackMode
PLAYBACK_MODE_POSVEL: PlaybackMode

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExecutionAccepted(_message.Message):
    __slots__ = ("execution_id",)
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    def __init__(self, execution_id: _Optional[str] = ...) -> None: ...

class StreamExecutionRequest(_message.Message):
    __slots__ = ("execution_id",)
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    def __init__(self, execution_id: _Optional[str] = ...) -> None: ...

class ExecutionStatus(_message.Message):
    __slots__ = ("execution_id", "state", "fraction", "robot_state", "error_message")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    FRACTION_FIELD_NUMBER: _ClassVar[int]
    ROBOT_STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    state: ExecState
    fraction: float
    robot_state: RobotState
    error_message: str
    def __init__(self, execution_id: _Optional[str] = ..., state: _Optional[_Union[ExecState, str]] = ..., fraction: _Optional[float] = ..., robot_state: _Optional[_Union[RobotState, _Mapping]] = ..., error_message: _Optional[str] = ...) -> None: ...

class CancelExecutionRequest(_message.Message):
    __slots__ = ("execution_id",)
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    def __init__(self, execution_id: _Optional[str] = ...) -> None: ...

class CancelExecutionResponse(_message.Message):
    __slots__ = ("cancelled",)
    CANCELLED_FIELD_NUMBER: _ClassVar[int]
    cancelled: bool
    def __init__(self, cancelled: _Optional[bool] = ...) -> None: ...

class AcquireControlRequest(_message.Message):
    __slots__ = ("client_id", "force")
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    client_id: str
    force: bool
    def __init__(self, client_id: _Optional[str] = ..., force: _Optional[bool] = ...) -> None: ...

class AcquireControlResponse(_message.Message):
    __slots__ = ("granted", "holder_client_id", "lease_token")
    GRANTED_FIELD_NUMBER: _ClassVar[int]
    HOLDER_CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    granted: bool
    holder_client_id: str
    lease_token: str
    def __init__(self, granted: _Optional[bool] = ..., holder_client_id: _Optional[str] = ..., lease_token: _Optional[str] = ...) -> None: ...

class ControlStatus(_message.Message):
    __slots__ = ("held", "holder_client_id", "estop_engaged", "watchdog_ok", "last_heartbeat_age_ms")
    HELD_FIELD_NUMBER: _ClassVar[int]
    HOLDER_CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    ESTOP_ENGAGED_FIELD_NUMBER: _ClassVar[int]
    WATCHDOG_OK_FIELD_NUMBER: _ClassVar[int]
    LAST_HEARTBEAT_AGE_MS_FIELD_NUMBER: _ClassVar[int]
    held: bool
    holder_client_id: str
    estop_engaged: bool
    watchdog_ok: bool
    last_heartbeat_age_ms: int
    def __init__(self, held: _Optional[bool] = ..., holder_client_id: _Optional[str] = ..., estop_engaged: _Optional[bool] = ..., watchdog_ok: _Optional[bool] = ..., last_heartbeat_age_ms: _Optional[int] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("ok", "server_time_ms")
    OK_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    server_time_ms: int
    def __init__(self, ok: _Optional[bool] = ..., server_time_ms: _Optional[int] = ...) -> None: ...

class EStopRequest(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class EStopResponse(_message.Message):
    __slots__ = ("engaged", "timestamp_ms")
    ENGAGED_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    engaged: bool
    timestamp_ms: int
    def __init__(self, engaged: _Optional[bool] = ..., timestamp_ms: _Optional[int] = ...) -> None: ...

class ClearEStopRequest(_message.Message):
    __slots__ = ("confirm",)
    CONFIRM_FIELD_NUMBER: _ClassVar[int]
    confirm: bool
    def __init__(self, confirm: _Optional[bool] = ...) -> None: ...

class JointLimit(_message.Message):
    __slots__ = ("name", "pos_min", "pos_max", "vel_max", "torque_max")
    NAME_FIELD_NUMBER: _ClassVar[int]
    POS_MIN_FIELD_NUMBER: _ClassVar[int]
    POS_MAX_FIELD_NUMBER: _ClassVar[int]
    VEL_MAX_FIELD_NUMBER: _ClassVar[int]
    TORQUE_MAX_FIELD_NUMBER: _ClassVar[int]
    name: str
    pos_min: float
    pos_max: float
    vel_max: float
    torque_max: float
    def __init__(self, name: _Optional[str] = ..., pos_min: _Optional[float] = ..., pos_max: _Optional[float] = ..., vel_max: _Optional[float] = ..., torque_max: _Optional[float] = ...) -> None: ...

class GripperLimit(_message.Message):
    __slots__ = ("pos_min", "pos_max", "vel_max", "torque_max")
    POS_MIN_FIELD_NUMBER: _ClassVar[int]
    POS_MAX_FIELD_NUMBER: _ClassVar[int]
    VEL_MAX_FIELD_NUMBER: _ClassVar[int]
    TORQUE_MAX_FIELD_NUMBER: _ClassVar[int]
    pos_min: float
    pos_max: float
    vel_max: float
    torque_max: float
    def __init__(self, pos_min: _Optional[float] = ..., pos_max: _Optional[float] = ..., vel_max: _Optional[float] = ..., torque_max: _Optional[float] = ...) -> None: ...

class SoftLimits(_message.Message):
    __slots__ = ("joint_limits", "gripper_limit", "hardware_limits_enabled")
    JOINT_LIMITS_FIELD_NUMBER: _ClassVar[int]
    GRIPPER_LIMIT_FIELD_NUMBER: _ClassVar[int]
    HARDWARE_LIMITS_ENABLED_FIELD_NUMBER: _ClassVar[int]
    joint_limits: _containers.RepeatedCompositeFieldContainer[JointLimit]
    gripper_limit: GripperLimit
    hardware_limits_enabled: bool
    def __init__(self, joint_limits: _Optional[_Iterable[_Union[JointLimit, _Mapping]]] = ..., gripper_limit: _Optional[_Union[GripperLimit, _Mapping]] = ..., hardware_limits_enabled: _Optional[bool] = ...) -> None: ...

class SetZeroRequest(_message.Message):
    __slots__ = ("confirm", "motor_ids")
    CONFIRM_FIELD_NUMBER: _ClassVar[int]
    MOTOR_IDS_FIELD_NUMBER: _ClassVar[int]
    confirm: bool
    motor_ids: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, confirm: _Optional[bool] = ..., motor_ids: _Optional[_Iterable[int]] = ...) -> None: ...

class SetZeroResponse(_message.Message):
    __slots__ = ("accepted", "persisted", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    PERSISTED_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    persisted: bool
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., persisted: _Optional[bool] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class MotorState(_message.Message):
    __slots__ = ("name", "motor_id", "position", "velocity", "torque", "motor_time", "mode", "fault", "pos_limit_flag", "tor_limit_flag", "valid")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MOTOR_ID_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_FIELD_NUMBER: _ClassVar[int]
    TORQUE_FIELD_NUMBER: _ClassVar[int]
    MOTOR_TIME_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    FAULT_FIELD_NUMBER: _ClassVar[int]
    POS_LIMIT_FLAG_FIELD_NUMBER: _ClassVar[int]
    TOR_LIMIT_FLAG_FIELD_NUMBER: _ClassVar[int]
    VALID_FIELD_NUMBER: _ClassVar[int]
    name: str
    motor_id: int
    position: float
    velocity: float
    torque: float
    motor_time: float
    mode: int
    fault: int
    pos_limit_flag: int
    tor_limit_flag: int
    valid: bool
    def __init__(self, name: _Optional[str] = ..., motor_id: _Optional[int] = ..., position: _Optional[float] = ..., velocity: _Optional[float] = ..., torque: _Optional[float] = ..., motor_time: _Optional[float] = ..., mode: _Optional[int] = ..., fault: _Optional[int] = ..., pos_limit_flag: _Optional[int] = ..., tor_limit_flag: _Optional[int] = ..., valid: _Optional[bool] = ...) -> None: ...

class JointState(_message.Message):
    __slots__ = ("joints", "timestamp_ms")
    JOINTS_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    joints: _containers.RepeatedCompositeFieldContainer[MotorState]
    timestamp_ms: int
    def __init__(self, joints: _Optional[_Iterable[_Union[MotorState, _Mapping]]] = ..., timestamp_ms: _Optional[int] = ...) -> None: ...

class GripperState(_message.Message):
    __slots__ = ("state", "timestamp_ms")
    STATE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    state: MotorState
    timestamp_ms: int
    def __init__(self, state: _Optional[_Union[MotorState, _Mapping]] = ..., timestamp_ms: _Optional[int] = ...) -> None: ...

class RobotState(_message.Message):
    __slots__ = ("joint", "gripper", "age_ms", "estop_engaged")
    JOINT_FIELD_NUMBER: _ClassVar[int]
    GRIPPER_FIELD_NUMBER: _ClassVar[int]
    AGE_MS_FIELD_NUMBER: _ClassVar[int]
    ESTOP_ENGAGED_FIELD_NUMBER: _ClassVar[int]
    joint: JointState
    gripper: GripperState
    age_ms: int
    estop_engaged: bool
    def __init__(self, joint: _Optional[_Union[JointState, _Mapping]] = ..., gripper: _Optional[_Union[GripperState, _Mapping]] = ..., age_ms: _Optional[int] = ..., estop_engaged: _Optional[bool] = ...) -> None: ...

class StreamStateRequest(_message.Message):
    __slots__ = ("rate_hz", "joints", "gripper")
    RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    JOINTS_FIELD_NUMBER: _ClassVar[int]
    GRIPPER_FIELD_NUMBER: _ClassVar[int]
    rate_hz: float
    joints: bool
    gripper: bool
    def __init__(self, rate_hz: _Optional[float] = ..., joints: _Optional[bool] = ..., gripper: _Optional[bool] = ...) -> None: ...

class CheckReachedRequest(_message.Message):
    __slots__ = ("target_positions", "tolerance")
    TARGET_POSITIONS_FIELD_NUMBER: _ClassVar[int]
    TOLERANCE_FIELD_NUMBER: _ClassVar[int]
    target_positions: _containers.RepeatedScalarFieldContainer[float]
    tolerance: float
    def __init__(self, target_positions: _Optional[_Iterable[float]] = ..., tolerance: _Optional[float] = ...) -> None: ...

class CheckReachedResponse(_message.Message):
    __slots__ = ("reached", "errors")
    REACHED_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    reached: bool
    errors: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, reached: _Optional[bool] = ..., errors: _Optional[_Iterable[float]] = ...) -> None: ...

class JointMoveRequest(_message.Message):
    __slots__ = ("positions", "velocities", "max_torque", "wait", "tolerance", "timeout_s")
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    VELOCITIES_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    TOLERANCE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[float]
    velocities: _containers.RepeatedScalarFieldContainer[float]
    max_torque: _containers.RepeatedScalarFieldContainer[float]
    wait: bool
    tolerance: float
    timeout_s: float
    def __init__(self, positions: _Optional[_Iterable[float]] = ..., velocities: _Optional[_Iterable[float]] = ..., max_torque: _Optional[_Iterable[float]] = ..., wait: _Optional[bool] = ..., tolerance: _Optional[float] = ..., timeout_s: _Optional[float] = ...) -> None: ...

class JointMoveResponse(_message.Message):
    __slots__ = ("accepted", "reached", "errors", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REACHED_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reached: bool
    errors: _containers.RepeatedScalarFieldContainer[float]
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., reached: _Optional[bool] = ..., errors: _Optional[_Iterable[float]] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class MoveJRequest(_message.Message):
    __slots__ = ("positions", "duration_s", "max_torque", "wait", "tolerance", "timeout_s")
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    WAIT_FIELD_NUMBER: _ClassVar[int]
    TOLERANCE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[float]
    duration_s: float
    max_torque: _containers.RepeatedScalarFieldContainer[float]
    wait: bool
    tolerance: float
    timeout_s: float
    def __init__(self, positions: _Optional[_Iterable[float]] = ..., duration_s: _Optional[float] = ..., max_torque: _Optional[_Iterable[float]] = ..., wait: _Optional[bool] = ..., tolerance: _Optional[float] = ..., timeout_s: _Optional[float] = ...) -> None: ...

class MoveJResponse(_message.Message):
    __slots__ = ("accepted", "reached", "errors", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REACHED_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reached: bool
    errors: _containers.RepeatedScalarFieldContainer[float]
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., reached: _Optional[bool] = ..., errors: _Optional[_Iterable[float]] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class JointJogCommand(_message.Message):
    __slots__ = ("velocities",)
    VELOCITIES_FIELD_NUMBER: _ClassVar[int]
    velocities: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, velocities: _Optional[_Iterable[float]] = ...) -> None: ...

class JointJogFeedback(_message.Message):
    __slots__ = ("joint_state", "limit_hit")
    JOINT_STATE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_HIT_FIELD_NUMBER: _ClassVar[int]
    joint_state: JointState
    limit_hit: _containers.RepeatedScalarFieldContainer[bool]
    def __init__(self, joint_state: _Optional[_Union[JointState, _Mapping]] = ..., limit_hit: _Optional[_Iterable[bool]] = ...) -> None: ...

class JointMITCommand(_message.Message):
    __slots__ = ("positions", "velocities", "torques", "kp", "kd")
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    VELOCITIES_FIELD_NUMBER: _ClassVar[int]
    TORQUES_FIELD_NUMBER: _ClassVar[int]
    KP_FIELD_NUMBER: _ClassVar[int]
    KD_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[float]
    velocities: _containers.RepeatedScalarFieldContainer[float]
    torques: _containers.RepeatedScalarFieldContainer[float]
    kp: _containers.RepeatedScalarFieldContainer[float]
    kd: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, positions: _Optional[_Iterable[float]] = ..., velocities: _Optional[_Iterable[float]] = ..., torques: _Optional[_Iterable[float]] = ..., kp: _Optional[_Iterable[float]] = ..., kd: _Optional[_Iterable[float]] = ...) -> None: ...

class JointMITFeedback(_message.Message):
    __slots__ = ("joint_state",)
    JOINT_STATE_FIELD_NUMBER: _ClassVar[int]
    joint_state: JointState
    def __init__(self, joint_state: _Optional[_Union[JointState, _Mapping]] = ...) -> None: ...

class GripperMoveRequest(_message.Message):
    __slots__ = ("position", "velocity", "max_torque")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    position: float
    velocity: float
    max_torque: float
    def __init__(self, position: _Optional[float] = ..., velocity: _Optional[float] = ..., max_torque: _Optional[float] = ...) -> None: ...

class GripperOpenRequest(_message.Message):
    __slots__ = ("position", "velocity", "max_torque")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    position: float
    velocity: float
    max_torque: float
    def __init__(self, position: _Optional[float] = ..., velocity: _Optional[float] = ..., max_torque: _Optional[float] = ...) -> None: ...

class GripperCloseRequest(_message.Message):
    __slots__ = ("position", "velocity", "max_torque")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    position: float
    velocity: float
    max_torque: float
    def __init__(self, position: _Optional[float] = ..., velocity: _Optional[float] = ..., max_torque: _Optional[float] = ...) -> None: ...

class GripperMoveResponse(_message.Message):
    __slots__ = ("accepted", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class GripperMITCommand(_message.Message):
    __slots__ = ("position", "velocity", "torque", "kp", "kd")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_FIELD_NUMBER: _ClassVar[int]
    TORQUE_FIELD_NUMBER: _ClassVar[int]
    KP_FIELD_NUMBER: _ClassVar[int]
    KD_FIELD_NUMBER: _ClassVar[int]
    position: float
    velocity: float
    torque: float
    kp: float
    kd: float
    def __init__(self, position: _Optional[float] = ..., velocity: _Optional[float] = ..., torque: _Optional[float] = ..., kp: _Optional[float] = ..., kd: _Optional[float] = ...) -> None: ...

class GripperMITResponse(_message.Message):
    __slots__ = ("accepted", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class JointAnglesOptional(_message.Message):
    __slots__ = ("joint_angles",)
    JOINT_ANGLES_FIELD_NUMBER: _ClassVar[int]
    joint_angles: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, joint_angles: _Optional[_Iterable[float]] = ...) -> None: ...

class ForwardKinematicsResponse(_message.Message):
    __slots__ = ("position", "rotation_matrix", "transform", "used_joint_angles")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    ROTATION_MATRIX_FIELD_NUMBER: _ClassVar[int]
    TRANSFORM_FIELD_NUMBER: _ClassVar[int]
    USED_JOINT_ANGLES_FIELD_NUMBER: _ClassVar[int]
    position: _containers.RepeatedScalarFieldContainer[float]
    rotation_matrix: _containers.RepeatedScalarFieldContainer[float]
    transform: _containers.RepeatedScalarFieldContainer[float]
    used_joint_angles: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, position: _Optional[_Iterable[float]] = ..., rotation_matrix: _Optional[_Iterable[float]] = ..., transform: _Optional[_Iterable[float]] = ..., used_joint_angles: _Optional[_Iterable[float]] = ...) -> None: ...

class JacobianResponse(_message.Message):
    __slots__ = ("matrix", "rows", "cols")
    MATRIX_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    COLS_FIELD_NUMBER: _ClassVar[int]
    matrix: _containers.RepeatedScalarFieldContainer[float]
    rows: int
    cols: int
    def __init__(self, matrix: _Optional[_Iterable[float]] = ..., rows: _Optional[int] = ..., cols: _Optional[int] = ...) -> None: ...

class ManipulabilityResponse(_message.Message):
    __slots__ = ("mu",)
    MU_FIELD_NUMBER: _ClassVar[int]
    mu: float
    def __init__(self, mu: _Optional[float] = ...) -> None: ...

class RPY(_message.Message):
    __slots__ = ("roll", "pitch", "yaw")
    ROLL_FIELD_NUMBER: _ClassVar[int]
    PITCH_FIELD_NUMBER: _ClassVar[int]
    YAW_FIELD_NUMBER: _ClassVar[int]
    roll: float
    pitch: float
    yaw: float
    def __init__(self, roll: _Optional[float] = ..., pitch: _Optional[float] = ..., yaw: _Optional[float] = ...) -> None: ...

class RotationMatrix(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, values: _Optional[_Iterable[float]] = ...) -> None: ...

class CartesianPose(_message.Message):
    __slots__ = ("position", "rpy", "matrix")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    RPY_FIELD_NUMBER: _ClassVar[int]
    MATRIX_FIELD_NUMBER: _ClassVar[int]
    position: _containers.RepeatedScalarFieldContainer[float]
    rpy: RPY
    matrix: RotationMatrix
    def __init__(self, position: _Optional[_Iterable[float]] = ..., rpy: _Optional[_Union[RPY, _Mapping]] = ..., matrix: _Optional[_Union[RotationMatrix, _Mapping]] = ...) -> None: ...

class InverseKinematicsRequest(_message.Message):
    __slots__ = ("target", "init_q", "max_iter", "eps", "damping", "adaptive_damping", "multi_init", "num_attempts", "timeout_s")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    INIT_Q_FIELD_NUMBER: _ClassVar[int]
    MAX_ITER_FIELD_NUMBER: _ClassVar[int]
    EPS_FIELD_NUMBER: _ClassVar[int]
    DAMPING_FIELD_NUMBER: _ClassVar[int]
    ADAPTIVE_DAMPING_FIELD_NUMBER: _ClassVar[int]
    MULTI_INIT_FIELD_NUMBER: _ClassVar[int]
    NUM_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    target: CartesianPose
    init_q: _containers.RepeatedScalarFieldContainer[float]
    max_iter: int
    eps: float
    damping: float
    adaptive_damping: bool
    multi_init: bool
    num_attempts: int
    timeout_s: float
    def __init__(self, target: _Optional[_Union[CartesianPose, _Mapping]] = ..., init_q: _Optional[_Iterable[float]] = ..., max_iter: _Optional[int] = ..., eps: _Optional[float] = ..., damping: _Optional[float] = ..., adaptive_damping: _Optional[bool] = ..., multi_init: _Optional[bool] = ..., num_attempts: _Optional[int] = ..., timeout_s: _Optional[float] = ...) -> None: ...

class InverseKinematicsResponse(_message.Message):
    __slots__ = ("found", "joint_angles", "error", "timeout")
    FOUND_FIELD_NUMBER: _ClassVar[int]
    JOINT_ANGLES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    found: bool
    joint_angles: _containers.RepeatedScalarFieldContainer[float]
    error: float
    timeout: bool
    def __init__(self, found: _Optional[bool] = ..., joint_angles: _Optional[_Iterable[float]] = ..., error: _Optional[float] = ..., timeout: _Optional[bool] = ...) -> None: ...

class PlanCartesianPathRequest(_message.Message):
    __slots__ = ("waypoints", "avoid_collisions")
    WAYPOINTS_FIELD_NUMBER: _ClassVar[int]
    AVOID_COLLISIONS_FIELD_NUMBER: _ClassVar[int]
    waypoints: _containers.RepeatedCompositeFieldContainer[CartesianPose]
    avoid_collisions: bool
    def __init__(self, waypoints: _Optional[_Iterable[_Union[CartesianPose, _Mapping]]] = ..., avoid_collisions: _Optional[bool] = ...) -> None: ...

class JointTrajectoryPoint(_message.Message):
    __slots__ = ("positions", "velocities", "timestamp_s")
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    VELOCITIES_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_S_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[float]
    velocities: _containers.RepeatedScalarFieldContainer[float]
    timestamp_s: float
    def __init__(self, positions: _Optional[_Iterable[float]] = ..., velocities: _Optional[_Iterable[float]] = ..., timestamp_s: _Optional[float] = ...) -> None: ...

class PlanCartesianPathResponse(_message.Message):
    __slots__ = ("joint_trajectory", "fraction")
    JOINT_TRAJECTORY_FIELD_NUMBER: _ClassVar[int]
    FRACTION_FIELD_NUMBER: _ClassVar[int]
    joint_trajectory: _containers.RepeatedCompositeFieldContainer[JointTrajectoryPoint]
    fraction: float
    def __init__(self, joint_trajectory: _Optional[_Iterable[_Union[JointTrajectoryPoint, _Mapping]]] = ..., fraction: _Optional[float] = ...) -> None: ...

class MoveLRequest(_message.Message):
    __slots__ = ("target", "duration_s", "use_spline", "max_torque")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    USE_SPLINE_FIELD_NUMBER: _ClassVar[int]
    MAX_TORQUE_FIELD_NUMBER: _ClassVar[int]
    target: CartesianPose
    duration_s: float
    use_spline: bool
    max_torque: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, target: _Optional[_Union[CartesianPose, _Mapping]] = ..., duration_s: _Optional[float] = ..., use_spline: _Optional[bool] = ..., max_torque: _Optional[_Iterable[float]] = ...) -> None: ...

class CartesianJogCommand(_message.Message):
    __slots__ = ("linear_velocity", "angular_velocity", "damping")
    LINEAR_VELOCITY_FIELD_NUMBER: _ClassVar[int]
    ANGULAR_VELOCITY_FIELD_NUMBER: _ClassVar[int]
    DAMPING_FIELD_NUMBER: _ClassVar[int]
    linear_velocity: _containers.RepeatedScalarFieldContainer[float]
    angular_velocity: _containers.RepeatedScalarFieldContainer[float]
    damping: float
    def __init__(self, linear_velocity: _Optional[_Iterable[float]] = ..., angular_velocity: _Optional[_Iterable[float]] = ..., damping: _Optional[float] = ...) -> None: ...

class CartesianJogFeedback(_message.Message):
    __slots__ = ("joint_state", "manipulability")
    JOINT_STATE_FIELD_NUMBER: _ClassVar[int]
    MANIPULABILITY_FIELD_NUMBER: _ClassVar[int]
    joint_state: JointState
    manipulability: float
    def __init__(self, joint_state: _Optional[_Union[JointState, _Mapping]] = ..., manipulability: _Optional[float] = ...) -> None: ...

class DynamicsQueryRequest(_message.Message):
    __slots__ = ("term", "q", "v", "a", "fc", "fv", "vel_threshold")
    TERM_FIELD_NUMBER: _ClassVar[int]
    Q_FIELD_NUMBER: _ClassVar[int]
    V_FIELD_NUMBER: _ClassVar[int]
    A_FIELD_NUMBER: _ClassVar[int]
    FC_FIELD_NUMBER: _ClassVar[int]
    FV_FIELD_NUMBER: _ClassVar[int]
    VEL_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    term: DynamicsTerm
    q: _containers.RepeatedScalarFieldContainer[float]
    v: _containers.RepeatedScalarFieldContainer[float]
    a: _containers.RepeatedScalarFieldContainer[float]
    fc: _containers.RepeatedScalarFieldContainer[float]
    fv: _containers.RepeatedScalarFieldContainer[float]
    vel_threshold: float
    def __init__(self, term: _Optional[_Union[DynamicsTerm, str]] = ..., q: _Optional[_Iterable[float]] = ..., v: _Optional[_Iterable[float]] = ..., a: _Optional[_Iterable[float]] = ..., fc: _Optional[_Iterable[float]] = ..., fv: _Optional[_Iterable[float]] = ..., vel_threshold: _Optional[float] = ...) -> None: ...

class DynamicsQueryResponse(_message.Message):
    __slots__ = ("gravity", "coriolis_matrix", "coriolis_vector", "mass_matrix", "inertia_terms", "inverse_dynamics", "friction_compensation")
    GRAVITY_FIELD_NUMBER: _ClassVar[int]
    CORIOLIS_MATRIX_FIELD_NUMBER: _ClassVar[int]
    CORIOLIS_VECTOR_FIELD_NUMBER: _ClassVar[int]
    MASS_MATRIX_FIELD_NUMBER: _ClassVar[int]
    INERTIA_TERMS_FIELD_NUMBER: _ClassVar[int]
    INVERSE_DYNAMICS_FIELD_NUMBER: _ClassVar[int]
    FRICTION_COMPENSATION_FIELD_NUMBER: _ClassVar[int]
    gravity: _containers.RepeatedScalarFieldContainer[float]
    coriolis_matrix: _containers.RepeatedScalarFieldContainer[float]
    coriolis_vector: _containers.RepeatedScalarFieldContainer[float]
    mass_matrix: _containers.RepeatedScalarFieldContainer[float]
    inertia_terms: _containers.RepeatedScalarFieldContainer[float]
    inverse_dynamics: _containers.RepeatedScalarFieldContainer[float]
    friction_compensation: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, gravity: _Optional[_Iterable[float]] = ..., coriolis_matrix: _Optional[_Iterable[float]] = ..., coriolis_vector: _Optional[_Iterable[float]] = ..., mass_matrix: _Optional[_Iterable[float]] = ..., inertia_terms: _Optional[_Iterable[float]] = ..., inverse_dynamics: _Optional[_Iterable[float]] = ..., friction_compensation: _Optional[_Iterable[float]] = ...) -> None: ...

class WaypointSpec(_message.Message):
    __slots__ = ("positions", "velocities")
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    VELOCITIES_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[float]
    velocities: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, positions: _Optional[_Iterable[float]] = ..., velocities: _Optional[_Iterable[float]] = ...) -> None: ...

class RunJointTrajectoryRequest(_message.Message):
    __slots__ = ("waypoints", "durations")
    WAYPOINTS_FIELD_NUMBER: _ClassVar[int]
    DURATIONS_FIELD_NUMBER: _ClassVar[int]
    waypoints: _containers.RepeatedCompositeFieldContainer[WaypointSpec]
    durations: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, waypoints: _Optional[_Iterable[_Union[WaypointSpec, _Mapping]]] = ..., durations: _Optional[_Iterable[float]] = ...) -> None: ...

class TeachStartRequest(_message.Message):
    __slots__ = ("kp", "kd", "fc", "fv")
    KP_FIELD_NUMBER: _ClassVar[int]
    KD_FIELD_NUMBER: _ClassVar[int]
    FC_FIELD_NUMBER: _ClassVar[int]
    FV_FIELD_NUMBER: _ClassVar[int]
    kp: _containers.RepeatedScalarFieldContainer[float]
    kd: _containers.RepeatedScalarFieldContainer[float]
    fc: _containers.RepeatedScalarFieldContainer[float]
    fv: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, kp: _Optional[_Iterable[float]] = ..., kd: _Optional[_Iterable[float]] = ..., fc: _Optional[_Iterable[float]] = ..., fv: _Optional[_Iterable[float]] = ...) -> None: ...

class TeachStartResponse(_message.Message):
    __slots__ = ("accepted", "reject_reason")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REJECT_REASON_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reject_reason: str
    def __init__(self, accepted: _Optional[bool] = ..., reject_reason: _Optional[str] = ...) -> None: ...

class TeachStopResponse(_message.Message):
    __slots__ = ("accepted",)
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    def __init__(self, accepted: _Optional[bool] = ...) -> None: ...

class TeachRecordStartRequest(_message.Message):
    __slots__ = ("path", "flush_interval")
    PATH_FIELD_NUMBER: _ClassVar[int]
    FLUSH_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    path: str
    flush_interval: float
    def __init__(self, path: _Optional[str] = ..., flush_interval: _Optional[float] = ...) -> None: ...

class TeachRecordStartResponse(_message.Message):
    __slots__ = ("accepted", "path")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    path: str
    def __init__(self, accepted: _Optional[bool] = ..., path: _Optional[str] = ...) -> None: ...

class TeachRecordStopResponse(_message.Message):
    __slots__ = ("accepted", "saved_path", "frame_count")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    SAVED_PATH_FIELD_NUMBER: _ClassVar[int]
    FRAME_COUNT_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    saved_path: str
    frame_count: int
    def __init__(self, accepted: _Optional[bool] = ..., saved_path: _Optional[str] = ..., frame_count: _Optional[int] = ...) -> None: ...

class TeachPlayRequest(_message.Message):
    __slots__ = ("path", "kp", "kd", "fc", "fv", "vel_threshold", "tau_limit", "gripper_kp", "gripper_kd", "playback_dt", "smooth_window", "mode")
    PATH_FIELD_NUMBER: _ClassVar[int]
    KP_FIELD_NUMBER: _ClassVar[int]
    KD_FIELD_NUMBER: _ClassVar[int]
    FC_FIELD_NUMBER: _ClassVar[int]
    FV_FIELD_NUMBER: _ClassVar[int]
    VEL_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    TAU_LIMIT_FIELD_NUMBER: _ClassVar[int]
    GRIPPER_KP_FIELD_NUMBER: _ClassVar[int]
    GRIPPER_KD_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_DT_FIELD_NUMBER: _ClassVar[int]
    SMOOTH_WINDOW_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    path: str
    kp: _containers.RepeatedScalarFieldContainer[float]
    kd: _containers.RepeatedScalarFieldContainer[float]
    fc: _containers.RepeatedScalarFieldContainer[float]
    fv: _containers.RepeatedScalarFieldContainer[float]
    vel_threshold: float
    tau_limit: _containers.RepeatedScalarFieldContainer[float]
    gripper_kp: float
    gripper_kd: float
    playback_dt: float
    smooth_window: int
    mode: PlaybackMode
    def __init__(self, path: _Optional[str] = ..., kp: _Optional[_Iterable[float]] = ..., kd: _Optional[_Iterable[float]] = ..., fc: _Optional[_Iterable[float]] = ..., fv: _Optional[_Iterable[float]] = ..., vel_threshold: _Optional[float] = ..., tau_limit: _Optional[_Iterable[float]] = ..., gripper_kp: _Optional[float] = ..., gripper_kd: _Optional[float] = ..., playback_dt: _Optional[float] = ..., smooth_window: _Optional[int] = ..., mode: _Optional[_Union[PlaybackMode, str]] = ...) -> None: ...

class TeachFileInfo(_message.Message):
    __slots__ = ("path", "recorded_at", "duration_s", "frame_count")
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECORDED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    FRAME_COUNT_FIELD_NUMBER: _ClassVar[int]
    path: str
    recorded_at: int
    duration_s: float
    frame_count: int
    def __init__(self, path: _Optional[str] = ..., recorded_at: _Optional[int] = ..., duration_s: _Optional[float] = ..., frame_count: _Optional[int] = ...) -> None: ...

class TeachListResponse(_message.Message):
    __slots__ = ("files",)
    FILES_FIELD_NUMBER: _ClassVar[int]
    files: _containers.RepeatedCompositeFieldContainer[TeachFileInfo]
    def __init__(self, files: _Optional[_Iterable[_Union[TeachFileInfo, _Mapping]]] = ...) -> None: ...

class DaemonStatus(_message.Message):
    __slots__ = ("version", "sim", "control_hz", "uptime_ms", "sdk_version", "estop_latch_hazard_present", "hardware_connected")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    SIM_FIELD_NUMBER: _ClassVar[int]
    CONTROL_HZ_FIELD_NUMBER: _ClassVar[int]
    UPTIME_MS_FIELD_NUMBER: _ClassVar[int]
    SDK_VERSION_FIELD_NUMBER: _ClassVar[int]
    ESTOP_LATCH_HAZARD_PRESENT_FIELD_NUMBER: _ClassVar[int]
    HARDWARE_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    version: str
    sim: bool
    control_hz: float
    uptime_ms: int
    sdk_version: str
    estop_latch_hazard_present: bool
    hardware_connected: bool
    def __init__(self, version: _Optional[str] = ..., sim: _Optional[bool] = ..., control_hz: _Optional[float] = ..., uptime_ms: _Optional[int] = ..., sdk_version: _Optional[str] = ..., estop_latch_hazard_present: _Optional[bool] = ..., hardware_connected: _Optional[bool] = ...) -> None: ...
