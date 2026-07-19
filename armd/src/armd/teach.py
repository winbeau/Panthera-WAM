"""拖动示教轨迹的非阻塞录制、校验与 SDK 兼容重采样。"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .hardware_loop import CachedRobotState

DEFAULT_TEACH_DIR = Path("~/.local/share/panthera/teach")
_CLOSE = object()


@dataclass(frozen=True, slots=True)
class PlaybackFrame:
    timestamp_s: float
    position: np.ndarray
    velocity: np.ndarray
    gripper_position: float | None
    gripper_velocity: float


@dataclass(frozen=True, slots=True)
class TeachFile:
    path: Path
    recorded_at_ms: int
    duration_s: float
    frame_count: int


class TrajectoryRecorder:
    """HardwareLoop 只入队，文件 I/O 在独立 writer 线程完成。"""

    def __init__(self, path: Path, *, flush_interval: float = 0.2) -> None:
        if flush_interval <= 0 or not np.isfinite(flush_interval):
            raise ValueError("flush_interval 必须是正有限数值")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.flush_interval = float(flush_interval)
        self._fd = path.open("w", encoding="utf-8")
        self._queue: queue.SimpleQueue[dict[str, Any] | object] = queue.SimpleQueue()
        self._started_at: float | None = None
        self._closed = False
        self._frame_count = 0
        self._writer_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._write_loop,
            name="panthera-teach-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def writer_error(self) -> BaseException | None:
        return self._writer_error

    def record(self, state: CachedRobotState) -> None:
        if self._closed:
            return
        if self._writer_error is not None:
            raise RuntimeError("示教轨迹 writer 已失败") from self._writer_error
        if len(state.motors) != 7 or not all(motor.valid for motor in state.motors):
            return
        if self._started_at is None:
            self._started_at = state.refreshed_at
        self._queue.put(
            {
                "t": max(0.0, state.refreshed_at - self._started_at),
                "pos": [float(motor.position) for motor in state.motors[:6]],
                "vel": [float(motor.velocity) for motor in state.motors[:6]],
                "gripper_pos": float(state.motors[6].position),
                "gripper_vel": float(state.motors[6].velocity),
            }
        )

    def close(self) -> int:
        if not self._closed:
            self._closed = True
            self._queue.put(_CLOSE)
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise TimeoutError("示教轨迹 writer 停止超时")
        if self._writer_error is not None:
            raise RuntimeError("示教轨迹保存失败") from self._writer_error
        return self._frame_count

    def _write_loop(self) -> None:
        last_flush = time.monotonic()
        try:
            while True:
                item = self._queue.get()
                if item is _CLOSE:
                    break
                assert isinstance(item, dict)
                self._fd.write(json.dumps(item, ensure_ascii=False) + "\n")
                self._frame_count += 1
                now = time.monotonic()
                if now - last_flush >= self.flush_interval:
                    self._fd.flush()
                    last_flush = now
            self._fd.flush()
        except BaseException as exc:
            self._writer_error = exc
        finally:
            self._fd.close()


class TeachStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.environ.get("PANTHERA_TEACH_DIR") or DEFAULT_TEACH_DIR
        self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def recording_path(self, requested: str) -> Path:
        name = requested.strip() or time.strftime("trajectory_%Y%m%d_%H%M%S.jsonl")
        return self._inside_root(name)

    def existing_path(self, requested: str) -> Path:
        if not requested.strip():
            raise ValueError("示教轨迹 path 不能为空")
        path = self._inside_root(requested)
        if not path.is_file():
            raise FileNotFoundError(f"示教轨迹不存在: {path}")
        return path

    def list_files(self) -> list[TeachFile]:
        files: list[TeachFile] = []
        for path in self.root.rglob("*.jsonl"):
            try:
                frames = load_raw_frames(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            duration = 0.0 if not frames else max(0.0, float(frames[-1]["t"]) - float(frames[0]["t"]))
            stat = path.stat()
            files.append(
                TeachFile(
                    path=path.resolve(),
                    recorded_at_ms=stat.st_mtime_ns // 1_000_000,
                    duration_s=duration,
                    frame_count=len(frames),
                )
            )
        return sorted(files, key=lambda item: item.recorded_at_ms, reverse=True)

    def _inside_root(self, requested: str) -> Path:
        raw = Path(requested).expanduser()
        candidate = (raw if raw.is_absolute() else self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError(f"示教轨迹必须位于目录内: {self.root}")
        return candidate


def load_raw_frames(path: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fd:
        for line_number, line in enumerate(fd, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
            _validate_raw_frame(item, line_number)
            frames.append(item)
    if not frames:
        raise ValueError("示教轨迹为空")
    return frames


def prepare_playback_frames(
    frames: list[dict[str, Any]],
    *,
    playback_dt: float = 0.01,
    smooth_window: int = 7,
) -> list[PlaybackFrame]:
    """逐步复现官方 Recorder._prepare_playback_frames。"""
    if playback_dt <= 0 or not np.isfinite(playback_dt):
        raise ValueError("playback_dt 必须大于 0")
    if smooth_window < 1:
        raise ValueError("smooth_window 必须大于等于 1")

    timestamps = np.asarray([frame["t"] for frame in frames], dtype=np.float64)
    timestamps -= timestamps[0]
    positions = np.asarray([frame["pos"] for frame in frames], dtype=np.float64)
    keep = np.concatenate(([True], np.diff(timestamps) > 1e-6))
    timestamps = timestamps[keep]
    positions = positions[keep]
    kept = [frame for frame, include in zip(frames, keep, strict=True) if include]

    if len(timestamps) < 2:
        frame = kept[0]
        velocity = np.asarray(frame.get("vel", np.zeros(6)), dtype=np.float64)
        return [
            PlaybackFrame(
                timestamp_s=0.0,
                position=np.asarray(frame["pos"], dtype=np.float64),
                velocity=velocity,
                gripper_position=(
                    float(frame["gripper_pos"]) if "gripper_pos" in frame else None
                ),
                gripper_velocity=float(frame.get("gripper_vel", 0.0)),
            )
        ]

    new_t = np.arange(0.0, timestamps[-1] + playback_dt * 0.5, playback_dt)
    new_pos = np.column_stack(
        [np.interp(new_t, timestamps, positions[:, index]) for index in range(positions.shape[1])]
    )
    new_pos = moving_average(new_pos, smooth_window)
    new_vel = np.gradient(new_pos, new_t, axis=0)

    has_gripper = all("gripper_pos" in frame for frame in kept)
    if has_gripper:
        gripper_pos = np.asarray([frame["gripper_pos"] for frame in kept], dtype=np.float64)
        new_gripper_pos = np.interp(new_t, timestamps, gripper_pos)
        new_gripper_pos = moving_average(new_gripper_pos[:, None], smooth_window)[:, 0]
        new_gripper_vel = np.gradient(new_gripper_pos, new_t)
    else:
        new_gripper_pos = np.zeros_like(new_t)
        new_gripper_vel = np.zeros_like(new_t)

    return [
        PlaybackFrame(
            timestamp_s=float(timestamp),
            position=new_pos[index].copy(),
            velocity=new_vel[index].copy(),
            gripper_position=float(new_gripper_pos[index]) if has_gripper else None,
            gripper_velocity=float(new_gripper_vel[index]),
        )
        for index, timestamp in enumerate(new_t)
    ]


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return values
    if window % 2 == 0:
        window += 1
    pad = window // 2
    padded = np.pad(values, [(pad, pad), (0, 0)], mode="edge")
    kernel = np.ones(window) / window
    return np.apply_along_axis(lambda value: np.convolve(value, kernel, mode="valid"), 0, padded)


def _validate_raw_frame(item: Any, line_number: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
    timestamp = item.get("t")
    if not isinstance(timestamp, (int, float)) or not np.isfinite(timestamp):
        raise ValueError(f"第 {line_number} 行 t 必须是有限数值")
    position = np.asarray(item.get("pos"), dtype=np.float64)
    if position.shape != (6,) or not np.all(np.isfinite(position)):
        raise ValueError(f"第 {line_number} 行 pos 必须包含 6 个有限数值")
    if "vel" in item:
        velocity = np.asarray(item["vel"], dtype=np.float64)
        if velocity.shape != (6,) or not np.all(np.isfinite(velocity)):
            raise ValueError(f"第 {line_number} 行 vel 必须包含 6 个有限数值")
    for field in ("gripper_pos", "gripper_vel"):
        if field in item and not isinstance(item[field], (int, float)):
            raise ValueError(f"第 {line_number} 行 {field} 必须是数值")
        if field in item and not np.isfinite(item[field]):
            raise ValueError(f"第 {line_number} 行 {field} 必须是有限数值")
