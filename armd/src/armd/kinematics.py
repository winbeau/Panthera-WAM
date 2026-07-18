"""独立进程中的纯 pinocchio 运动学与笛卡尔路径计算。"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pinocchio as pin
import yaml
from scipy.interpolate import CubicSpline
from scipy.spatial.transform import Rotation


def default_sdk_root() -> Path:
    configured = os.environ.get("PANTHERA_SDK_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    repository_vendor = Path(__file__).resolve().parents[3] / "vendor" / "Panthera-HT_SDK"
    if repository_vendor.is_dir():
        return repository_vendor
    return (Path.home() / "Panthera-HT_SDK").resolve()


class KinematicsEngine:
    def __init__(self, *, sdk_root: str | Path, config_path: str | Path | None = None) -> None:
        root = Path(sdk_root).expanduser().resolve()
        config = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else root / "panthera_python" / "robot_param" / "Follower.yaml"
        )
        with config.open("r", encoding="utf-8") as file:
            settings = yaml.safe_load(file)
        urdf_path = (config.parent / settings["urdf"]["file_path"]).resolve()
        self.model = pin.buildModelFromUrdf(str(urdf_path))
        self.data = self.model.createData()
        self.joint_names = list(settings["kinematics"]["joint_names"])
        self.end_effector_frame_id = self.model.getFrameId(settings["urdf"]["end_effector_link"])
        self.joint_lower = np.asarray(settings["robot"]["joint_limits"]["lower"], dtype=np.float64)
        self.joint_upper = np.asarray(settings["robot"]["joint_limits"]["upper"], dtype=np.float64)
        self.velocity_limits = np.asarray(settings["robot"]["velocity_limits"], dtype=np.float64)
        self.acceleration_limits = np.asarray(settings["robot"]["acceleration_limits"], dtype=np.float64)
        moveit = settings["moveit_cartesian"]
        self.eef_step = float(moveit["eef_step"])
        self.jump_threshold = float(moveit["jump_threshold"])
        self.resample_dt = float(moveit["resample_dt"])

    def _model_q(self, joint_angles: np.ndarray) -> np.ndarray:
        values = np.asarray(joint_angles, dtype=np.float64)
        if values.shape != (6,):
            raise ValueError("joint_angles 必须包含 6 个数值")
        q = np.zeros(self.model.nq)
        for index, joint_name in enumerate(self.joint_names):
            joint_id = self.model.getJointId(joint_name)
            q[self.model.joints[joint_id].idx_q] = values[index]
        return q

    def _joint_q(self, model_q: np.ndarray) -> np.ndarray:
        return np.array(
            [model_q[self.model.joints[self.model.getJointId(name)].idx_q] for name in self.joint_names],
            dtype=np.float64,
        )

    def forward_kinematics(self, joint_angles: np.ndarray) -> dict[str, Any]:
        q = self._model_q(joint_angles)
        pin.forwardKinematics(self.model, self.data, q)
        pin.updateFramePlacements(self.model, self.data)
        transform = self.data.oMf[self.end_effector_frame_id]
        matrix = np.eye(4)
        matrix[:3, :3] = transform.rotation
        matrix[:3, 3] = transform.translation
        return {
            "position": transform.translation.copy(),
            "rotation": transform.rotation.copy(),
            "transform": matrix,
            "joint_angles": np.asarray(joint_angles, dtype=np.float64),
        }

    def jacobian(self, joint_angles: np.ndarray) -> np.ndarray:
        q = self._model_q(joint_angles)
        pin.forwardKinematics(self.model, self.data, q)
        pin.updateFramePlacements(self.model, self.data)
        full = pin.computeFrameJacobian(
            self.model,
            self.data,
            q,
            self.end_effector_frame_id,
            pin.LOCAL_WORLD_ALIGNED,
        )
        result = np.zeros((6, len(self.joint_names)))
        for index, joint_name in enumerate(self.joint_names):
            joint_id = self.model.getJointId(joint_name)
            result[:, index] = full[:, self.model.joints[joint_id].idx_v]
        return result

    def manipulability(self, joint_angles: np.ndarray) -> float:
        jacobian = self.jacobian(joint_angles)
        determinant = np.linalg.det(jacobian @ jacobian.T)
        return float(np.sqrt(max(determinant, 0.0)))

    def inverse_kinematics(
        self,
        *,
        target_position: np.ndarray,
        target_rotation: np.ndarray | None,
        init_q: np.ndarray,
        max_iter: int,
        eps: float,
        damping: float,
        adaptive_damping: bool,
        multi_init: bool,
        num_attempts: int,
    ) -> np.ndarray | None:
        if multi_init:
            initial = [init_q, np.zeros(6), (self.joint_lower + self.joint_upper) / 2]
            generator = np.random.default_rng(0)
            for _ in range(max(0, num_attempts - 3)):
                initial.append(generator.uniform(self.joint_lower, self.joint_upper))
        else:
            initial = [init_q]

        best_result: np.ndarray | None = None
        best_error = float("inf")
        for candidate in initial[:num_attempts]:
            result = self._inverse_single(
                target_position=target_position,
                target_rotation=target_rotation,
                init_q=np.asarray(candidate, dtype=np.float64),
                max_iter=max_iter,
                eps=eps,
                damping=damping,
                adaptive_damping=adaptive_damping,
            )
            if result is None:
                continue
            error = float(np.linalg.norm(self.forward_kinematics(result)["position"] - target_position))
            if error < best_error:
                best_result = result
                best_error = error
            if error < eps:
                return result
        return best_result

    def _inverse_single(
        self,
        *,
        target_position: np.ndarray,
        target_rotation: np.ndarray | None,
        init_q: np.ndarray,
        max_iter: int,
        eps: float,
        damping: float,
        adaptive_damping: bool,
    ) -> np.ndarray | None:
        rotation = np.eye(3) if target_rotation is None else np.asarray(target_rotation, dtype=np.float64)
        desired = pin.SE3(rotation, np.asarray(target_position, dtype=np.float64))
        q = self._model_q(init_q)
        for _ in range(max_iter):
            pin.forwardKinematics(self.model, self.data, q)
            pin.updateFramePlacements(self.model, self.data)
            delta = self.data.oMf[self.end_effector_frame_id].actInv(desired)
            error = pin.log(delta).vector
            error_norm = np.linalg.norm(error)
            if error_norm < eps:
                return self._joint_q(q)
            jacobian = pin.computeFrameJacobian(
                self.model,
                self.data,
                q,
                self.end_effector_frame_id,
                pin.LOCAL,
            )
            jacobian = -pin.Jlog6(delta.inverse()) @ jacobian
            effective_damping = damping * (1.0 + 1.0 / (error_norm + 0.1)) if adaptive_damping else damping
            try:
                alpha = np.linalg.solve(
                    jacobian @ jacobian.T + effective_damping**2 * np.eye(6),
                    error,
                )
            except np.linalg.LinAlgError:
                return None
            velocity = -jacobian.T @ alpha
            norm = np.linalg.norm(velocity)
            if norm > 10.0:
                velocity *= 10.0 / norm
            candidate = pin.integrate(self.model, q, velocity * 0.1)
            joint_candidate = self._joint_q(candidate)
            if np.any(joint_candidate < self.joint_lower) or np.any(joint_candidate > self.joint_upper):
                return None
            q = candidate
        return None

    @staticmethod
    def rotation_from_euler(roll: float, pitch: float, yaw: float) -> np.ndarray:
        return Rotation.from_euler("xyz", [roll, pitch, yaw]).as_matrix()

    def compute_cartesian_path(
        self,
        *,
        current_q: np.ndarray,
        waypoints: list[dict[str, np.ndarray]],
    ) -> tuple[list[np.ndarray], float]:
        if len(waypoints) < 2:
            raise ValueError("至少需要 2 个笛卡尔路径点")
        trajectory: list[np.ndarray] = []
        active_q = np.asarray(current_q, dtype=np.float64)
        for segment_index, (start, end) in enumerate(zip(waypoints, waypoints[1:], strict=False)):
            segment, success = self._interpolate_segment(start, end, active_q)
            if not success:
                steps = self._segment_steps(start, end)
                fraction = (segment_index + len(segment) / steps) / (len(waypoints) - 1)
                return trajectory, float(fraction)
            trajectory.extend(segment)
            if segment:
                active_q = segment[-1]
        return trajectory, 1.0

    def _interpolate_segment(
        self,
        start: dict[str, np.ndarray],
        end: dict[str, np.ndarray],
        init_q: np.ndarray,
    ) -> tuple[list[np.ndarray], bool]:
        steps = self._segment_steps(start, end)
        trajectory: list[np.ndarray] = []
        active_q = np.asarray(init_q, dtype=np.float64)
        start_rotation = Rotation.from_matrix(start["rotation"])
        end_rotation = Rotation.from_matrix(end["rotation"])
        start_quaternion = start_rotation.as_quat()
        end_quaternion = end_rotation.as_quat()
        if np.dot(start_quaternion, end_quaternion) < 0:
            end_quaternion = -end_quaternion
        for step in range(1, steps + 1):
            fraction = step / steps
            position = (1 - fraction) * start["position"] + fraction * end["position"]
            quaternion = (1 - fraction) * start_quaternion + fraction * end_quaternion
            quaternion /= np.linalg.norm(quaternion)
            result = self.inverse_kinematics(
                target_position=position,
                target_rotation=Rotation.from_quat(quaternion).as_matrix(),
                init_q=active_q,
                max_iter=1000,
                eps=1e-3,
                damping=1e-2,
                adaptive_damping=True,
                multi_init=False,
                num_attempts=1,
            )
            if result is None:
                return trajectory, False
            if trajectory and np.any(np.abs(result - active_q) > self.jump_threshold):
                return trajectory, False
            trajectory.append(result)
            active_q = result
        return trajectory, True

    def _segment_steps(self, start: dict[str, np.ndarray], end: dict[str, np.ndarray]) -> int:
        position_distance = np.linalg.norm(end["position"] - start["position"])
        rotation_delta = Rotation.from_matrix(end["rotation"]) * Rotation.from_matrix(start["rotation"]).inv()
        return max(
            1,
            int(np.ceil(position_distance / self.eef_step)),
            int(np.ceil(rotation_delta.magnitude() / 0.1)),
        )

    def time_parameterization(
        self,
        trajectory: list[np.ndarray],
        duration: float | None,
    ) -> list[float]:
        if len(trajectory) < 2:
            return [0.0]
        if duration is not None:
            return np.linspace(0.0, duration, len(trajectory)).tolist()
        timestamps = [0.0]
        for previous, current in zip(trajectory, trajectory[1:], strict=False):
            delta = np.abs(current - previous)
            velocity_time = float(np.max(delta / self.velocity_limits))
            acceleration_time = float(np.max(np.sqrt(2 * delta / self.acceleration_limits)))
            timestamps.append(timestamps[-1] + max(velocity_time, acceleration_time, 0.01))
        return timestamps

    def parameterize_trajectory(
        self,
        trajectory: list[np.ndarray],
        *,
        duration: float | None,
        use_spline: bool,
    ) -> tuple[list[np.ndarray], list[float], list[np.ndarray]]:
        timestamps = self.time_parameterization(trajectory, duration)
        for _ in range(6):
            if use_spline and trajectory:
                positions, sampled_timestamps, velocities = self.smooth_trajectory(
                    trajectory,
                    timestamps,
                )
            else:
                positions = trajectory
                sampled_timestamps = timestamps
                velocities = [np.zeros(6)]
                if len(trajectory) > 1:
                    values = np.asarray(trajectory)
                    velocities = np.gradient(values, np.asarray(timestamps), axis=0).tolist()

            scale = self._trajectory_limit_scale(positions, sampled_timestamps, velocities)
            if scale <= 1.0 + 1e-6:
                return positions, sampled_timestamps, [np.asarray(value) for value in velocities]
            if duration is not None:
                raise ValueError(
                    f"duration_s 过短，至少需要约 {duration * scale * 1.02:.3f}s 才能满足速度/加速度限位"
                )
            timestamps = [value * scale * 1.02 for value in timestamps]
        raise ValueError("轨迹时间参数化无法满足速度/加速度限位")

    def _trajectory_limit_scale(
        self,
        positions: list[np.ndarray],
        timestamps: list[float],
        velocities: list[np.ndarray],
    ) -> float:
        position_values = np.asarray(positions, dtype=np.float64)
        velocity_values = np.asarray(velocities, dtype=np.float64)
        time_values = np.asarray(timestamps, dtype=np.float64)
        if not np.all(np.isfinite(position_values)) or not np.all(np.isfinite(velocity_values)):
            raise ValueError("轨迹包含非有限数值")
        if np.any(position_values < self.joint_lower - 1e-9) or np.any(
            position_values > self.joint_upper + 1e-9
        ):
            raise ValueError("样条轨迹越过关节软限位")
        velocity_ratio = float(np.max(np.abs(velocity_values) / self.velocity_limits))
        acceleration_ratio = 0.0
        if len(time_values) >= 3:
            accelerations = np.gradient(velocity_values, time_values, axis=0)
            acceleration_ratio = float(np.sqrt(np.max(np.abs(accelerations) / self.acceleration_limits)))
        return max(1.0, velocity_ratio, acceleration_ratio)

    def smooth_trajectory(
        self,
        trajectory: list[np.ndarray],
        timestamps: list[float],
    ) -> tuple[list[np.ndarray], list[float], list[np.ndarray]]:
        if len(trajectory) < 2:
            return trajectory, [0.0], [np.zeros(6)]
        positions = np.asarray(trajectory, dtype=np.float64)
        time_values = np.asarray(timestamps, dtype=np.float64)
        splines = [CubicSpline(time_values, positions[:, index], bc_type="clamped") for index in range(6)]
        duration = time_values[-1] - time_values[0]
        sample_count = max(21, int(np.ceil(duration / self.resample_dt)) + 1)
        resampled = np.linspace(time_values[0], time_values[-1], sample_count)
        smooth_positions = [np.array([spline(value) for spline in splines]) for value in resampled]
        smooth_velocities = [np.array([spline(value, 1) for spline in splines]) for value in resampled]
        return smooth_positions, resampled.tolist(), smooth_velocities


_ENGINE: KinematicsEngine | None = None


def _initialize_worker(sdk_root: str, config_path: str | None) -> None:
    global _ENGINE
    _ENGINE = KinematicsEngine(sdk_root=sdk_root, config_path=config_path)


def _worker_call(operation: str, payload: dict[str, Any]) -> Any:
    if _ENGINE is None:
        raise RuntimeError("运动学 worker 尚未初始化")
    if operation == "fk":
        return _ENGINE.forward_kinematics(payload["q"])
    if operation == "jacobian":
        return _ENGINE.jacobian(payload["q"])
    if operation == "manipulability":
        return _ENGINE.manipulability(payload["q"])
    if operation == "ik":
        return _ENGINE.inverse_kinematics(**payload)
    if operation == "plan":
        trajectory, fraction = _ENGINE.compute_cartesian_path(
            current_q=payload["current_q"],
            waypoints=payload["waypoints"],
        )
        positions, timestamps, velocities = _ENGINE.parameterize_trajectory(
            trajectory,
            duration=payload.get("duration"),
            use_spline=payload.get("use_spline", True),
        )
        return {
            "positions": positions,
            "velocities": velocities,
            "timestamps": timestamps,
            "fraction": fraction,
        }
    raise ValueError(f"未知运动学操作: {operation}")


class KinematicsWorker:
    def __init__(
        self,
        *,
        sdk_root: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        root = Path(sdk_root or default_sdk_root()).expanduser().resolve()
        config = str(Path(config_path).expanduser().resolve()) if config_path is not None else None
        self._executor = ProcessPoolExecutor(
            max_workers=1,
            mp_context=mp.get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(str(root), config),
        )
        self._warmed = False
        self._warm_lock = asyncio.Lock()

    async def warm(self) -> None:
        if self._warmed:
            return
        async with self._warm_lock:
            if self._warmed:
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, _worker_call, "fk", {"q": np.zeros(6)})
            self._warmed = True

    async def call(self, operation: str, payload: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, _worker_call, operation, payload)
        self._warmed = True
        return result

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
