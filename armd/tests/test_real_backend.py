from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from armd.backend import (
    Backend,
    BackendClosedError,
    FrameMode,
    JointFrame,
    LimitViolationError,
    RealBackend,
    SdkAuditError,
    audit_sdk_source,
)


@dataclass
class FakeVersion:
    major: int = 4
    minor: int = 7
    patch: int = 3


@dataclass
class FakeState:
    ID: int
    position: float = 0.0
    velocity: float = 0.0
    torque: float = 0.0
    time: float = 100.0
    mode: int = int(FrameMode.STOP)
    fault: int = 0


class FakeMotor:
    def __init__(
        self,
        motor_id: int,
        events: list[tuple],
        *,
        position: float = 0.0,
        version: FakeVersion | None = None,
    ) -> None:
        self.motor_id = motor_id
        self.events = events
        self.state = FakeState(ID=motor_id, position=position)
        self.version = version or FakeVersion()
        self.version_reads = 0
        self.pos_limit_flag = 0
        self.tor_limit_flag = 0

    def get_motor_name(self) -> str:
        return f"joint{self.motor_id}"

    def get_current_motor_state(self) -> FakeState:
        return self.state

    def get_version(self) -> FakeVersion:
        self.version_reads += 1
        return self.version

    def velocity(self, velocity: float) -> None:
        self.events.append((self.motor_id, "velocity", velocity))

    def brake(self) -> None:
        self.events.append((self.motor_id, "brake"))

    def pos_vel_MAXtqe(self, position: float, velocity: float, max_torque: float) -> None:
        self.events.append((self.motor_id, "posvel", position, velocity, max_torque))

    def pos_vel_tqe_kp_kd(
        self,
        position: float,
        velocity: float,
        torque: float,
        kp: float,
        kd: float,
    ) -> None:
        self.events.append((self.motor_id, "mit", position, velocity, torque, kp, kd))


class FakeRobot:
    def __init__(self, *, version: FakeVersion | None = None) -> None:
        self.events: list[tuple] = []
        self.motors = [FakeMotor(index, self.events, version=version) for index in range(1, 8)]
        self.timeout_calls: list[int] = []
        self.get_motors_calls = 0
        self.flushes = 0
        self.state_queries = 0
        self.stop_calls = 0
        self.zero_all_calls = 0
        self.zero_selected_calls: list[list[int]] = []
        self.joint_limits = {
            "lower": np.array([-2.4, -0.1, -0.1, -1.6, -1.7, -2.5]),
            "upper": np.array([2.4, 3.2, 4.0, 1.6, 1.7, 2.5]),
        }
        self.gripper_limits = {"lower": 0.0, "upper": 2.0}
        self.velocity_limits = np.ones(6)
        self.acceleration_limits = np.full(6, 2.0)
        self.max_torque = np.array([21.0, 36.0, 36.0, 21.0, 10.0, 10.0])

    def get_motors(self):
        self.get_motors_calls += 1
        return list(self.motors)

    def set_timeout(self, timeout_ms: int) -> None:
        self.timeout_calls.append(timeout_ms)

    def send_get_motor_state_cmd(self) -> None:
        self.state_queries += 1

    def motor_send_cmd(self) -> None:
        self.flushes += 1

    def set_stop(self) -> None:
        self.stop_calls += 1

    def set_reset_zero(self) -> None:
        self.zero_all_calls += 1

    def set_reset_zero_motors(self, motor_indices: list[int]) -> None:
        self.zero_selected_calls.append(list(motor_indices))


def make_backend(robot: FakeRobot, *, timeout_ms: int = 150) -> RealBackend:
    module = SimpleNamespace(__version__="1.0.0", __cpp_sdk_version__="4.4.7")
    return RealBackend(
        sdk_root="/unused",
        motor_timeout_ms=timeout_ms,
        robot_factory=lambda _: robot,
        sdk_module=module,
        run_source_audit=False,
    )


def pos_vel_frame(*, first_position: float = 0.2) -> JointFrame:
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE,
        arm_position=np.array([first_position, 0.2, 0.3, -0.2, 0.1, -0.3]),
        arm_velocity=np.array([0.5] * 6),
        arm_max_torque=np.array([21.0, 36.0, 36.0, 21.0, 10.0, 10.0]),
        gripper_position=0.4,
        gripper_velocity=0.5,
        gripper_max_torque=0.5,
    )


def velocity_frame() -> JointFrame:
    return JointFrame(
        mode=FrameMode.VELOCITY,
        arm_position=np.zeros(6),
        arm_velocity=np.array([0.1, -0.2, 0.3, -0.4, 0.5, -0.6]),
        gripper_position=0.0,
        gripper_velocity=0.2,
    )


def mit_frame() -> JointFrame:
    return JointFrame(
        mode=FrameMode.POS_VEL_TQE_KP_KD,
        arm_position=np.array([0.2, 0.2, 0.3, -0.2, 0.1, -0.3]),
        arm_velocity=np.zeros(6),
        arm_torque=np.array([1.0, 2.0, 3.0, 4.0, 1.0, 1.0]),
        arm_kp=np.array([30.0, 50.0, 60.0, 25.0, 15.0, 10.0]),
        arm_kd=np.array([3.0, 5.0, 6.0, 2.5, 1.5, 1.0]),
        gripper_position=0.4,
        gripper_velocity=0.0,
        gripper_torque=0.2,
        gripper_kp=5.0,
        gripper_kd=0.5,
    )


def test_source_audit_detects_new_limit_call_site(tmp_path: Path) -> None:
    include = tmp_path / "panthera_cpp" / "motor_cpp" / "include" / "hardware"
    source = tmp_path / "panthera_cpp" / "motor_cpp" / "src" / "hardware"
    include.mkdir(parents=True)
    source.mkdir(parents=True)
    (include / "robot.hpp").write_text("void detect_motor_limit();\n", encoding="utf-8")
    robot_cpp = source / "robot.cpp"
    robot_cpp.write_text("void robot::detect_motor_limit() {}\n", encoding="utf-8")

    safe = audit_sdk_source(tmp_path)
    assert not safe.estop_latch_hazard_present

    robot_cpp.write_text(
        "void robot::detect_motor_limit() {}\nvoid robot::tick() { detect_motor_limit(); }\n",
        encoding="utf-8",
    )
    unsafe = audit_sdk_source(tmp_path)
    assert unsafe.estop_latch_hazard_present
    assert unsafe.detect_motor_limit_call_sites == ("panthera_cpp/motor_cpp/src/hardware/robot.cpp:2",)


def test_timeout_is_applied_once_during_init_without_redundant_flush() -> None:
    robot = FakeRobot()
    backend = make_backend(robot)

    assert isinstance(backend, Backend)
    assert robot.timeout_calls == [150]
    assert robot.flushes == 0
    assert backend.sdk_version == "python=1.0.0,cpp=4.4.7,motors=4.7.3,4.7.3,4.7.3,4.7.3,4.7.3,4.7.3,4.7.3"


@pytest.mark.parametrize(
    ("frame", "command"),
    [
        (pos_vel_frame(), "posvel"),
        (velocity_frame(), "velocity"),
        (mit_frame(), "mit"),
    ],
)
def test_full_seven_slot_frame_uses_one_mode_and_one_flush(frame: JointFrame, command: str) -> None:
    robot = FakeRobot()
    backend = make_backend(robot)

    backend.write_frame(frame)

    assert len(robot.events) == 7
    assert [event[0] for event in robot.events] == list(range(1, 8))
    assert {event[1] for event in robot.events} == {command}
    assert robot.flushes == 1


def test_refresh_replaces_stale_motor_handles_after_reconnect() -> None:
    robot = FakeRobot()
    backend = make_backend(robot)
    assert backend.read_all()[0].position == 0.0

    robot.motors = []
    backend.refresh_state()
    assert not backend.read_all()[0].valid

    robot.motors = [FakeMotor(index, robot.events, position=0.1 * index) for index in range(1, 8)]
    backend.refresh_state()
    states = backend.read_all()

    assert states[0].position == pytest.approx(0.1)
    assert states[6].position == pytest.approx(0.7)
    assert robot.state_queries == 1
    assert robot.flushes == 1


def test_each_state_cycle_refreshes_handles_once_and_versions_at_low_rate() -> None:
    robot = FakeRobot()
    backend = make_backend(robot)
    initial_get_motors_calls = robot.get_motors_calls
    initial_version_reads = sum(motor.version_reads for motor in robot.motors)

    backend.refresh_state()
    backend.read_all()

    assert robot.get_motors_calls == initial_get_motors_calls + 1
    assert sum(motor.version_reads for motor in robot.motors) == initial_version_reads


def test_state_mapping_preserves_sdk_fields_and_disconnected_sentinel() -> None:
    robot = FakeRobot()
    motor = robot.motors[2]
    motor.state = FakeState(
        ID=3,
        position=999.0,
        velocity=1.2,
        torque=-0.4,
        time=123.5,
        mode=int(FrameMode.POS_VEL_TQE),
        fault=7,
    )
    motor.pos_limit_flag = -1
    motor.tor_limit_flag = 1
    backend = make_backend(robot)

    state = backend.read_all()[2]

    assert state.name == "joint3"
    assert state.motor_id == 3
    assert not state.valid
    assert state.velocity == pytest.approx(1.2)
    assert state.torque == pytest.approx(-0.4)
    assert state.motor_time == pytest.approx(123.5)
    assert state.mode == FrameMode.POS_VEL_TQE
    assert state.fault == 7
    assert state.pos_limit_flag == -1
    assert state.tor_limit_flag == 1


def test_old_firmware_is_rejected_before_timeout_or_control() -> None:
    robot = FakeRobot(version=FakeVersion(4, 1, 9))

    with pytest.raises(SdkAuditError, match="状态查询可能退化"):
        make_backend(robot)

    assert robot.timeout_calls == []
    assert robot.events == []


def test_set_zero_preserves_persistence_and_sdk_index_semantics() -> None:
    robot = FakeRobot()
    backend = make_backend(robot)

    assert backend.set_zero() == (True, False, "")
    assert robot.zero_all_calls == 1
    assert robot.flushes == 1

    assert backend.set_zero([1, 7]) == (True, True, "")
    assert robot.zero_selected_calls == [[0, 6]]


def test_backend_rejects_limits_and_close_is_idempotent() -> None:
    robot = FakeRobot()
    backend = make_backend(robot)

    with pytest.raises(LimitViolationError, match=r"joint1.*上限"):
        backend.write_frame(pos_vel_frame(first_position=2.5))
    assert robot.events == []
    assert robot.flushes == 0

    backend.close()
    backend.close()
    with pytest.raises(BackendClosedError):
        backend.read_all()
