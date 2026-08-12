from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow.parquet as pq
from PIL import Image

from .schema import (
    ACTION_SEMANTICS,
    AXES,
    FPS,
    LEROBOT_CODEBASE_VERSION,
    LEROBOT_VERSION,
    SCHEMA_VERSION,
    schema_identity,
)

_REQUIRED_IDENTITY_FIELDS = (
    "dataset_id",
    "task_id",
    "calibration_version",
    "camera_mount_version",
    "roi_version",
    "action_units_version",
)
_REQUIRED_CAMERA_FIELDS = (
    "path",
    "sequence",
    "device_timestamp_raw",
    "device_timestamp_unit",
    "device_clock_domain",
    "host_receive_monotonic_ns",
    "host_publish_monotonic_ns",
    "estimated_capture_monotonic_ns",
    "timestamp_source",
    "timestamp_quality",
    "delta_ns",
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path, *, exclude: Iterable[str] = ()) -> str:
    excluded = set(exclude)
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _load_samples(path: Path) -> list[dict[str, Any]]:
    samples = pq.read_table(path).to_pylist()
    if not all(isinstance(sample, dict) for sample in samples):
        raise ValueError(f"samples parquet contains non-object rows: {path}")
    if len(samples) < 2:
        raise ValueError("at least two staging samples are required to construct q[t+1] actions")
    return samples


def _finite_vector(value: Any, *, field: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float32)
    if array.shape != (len(AXES),):
        raise ValueError(f"{field} must have shape ({len(AXES)},), got {array.shape}")
    if not np.isfinite(array).all():
        raise ValueError(f"{field} contains NaN or Inf")
    return array


def _required_int(mapping: dict[str, Any], field: str, *, context: str) -> int:
    if field not in mapping:
        raise ValueError(f"missing {context}.{field}")
    value = mapping[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}.{field} must be an integer")
    return value


def _optional_int(mapping: dict[str, Any], field: str) -> int:
    value = mapping.get(field)
    if value is None:
        return -1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    return value


def _optional_float(mapping: dict[str, Any], field: str) -> float:
    value = mapping.get(field)
    if value is None:
        return -1.0
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite or null")
    return result


def _require_zero_counts(mapping: Any, *, field: str) -> None:
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(f"{field} must be a non-empty object")
    nonzero = {key: value for key, value in mapping.items() if int(value) != 0}
    if nonzero:
        raise ValueError(f"{field} must contain only zero counts, got {nonzero}")


def _camera(sample: dict[str, Any], key: str, *, index: int) -> dict[str, Any]:
    value = sample.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"sample[{index}].{key} must be an object")
    missing = [field for field in _REQUIRED_CAMERA_FIELDS if field not in value]
    if missing:
        raise ValueError(f"sample[{index}].{key} is missing {missing}")
    return value


def _state(sample: dict[str, Any], *, index: int) -> dict[str, Any]:
    value = sample.get("state")
    if not isinstance(value, dict):
        raise ValueError(f"sample[{index}].state must be an object")
    _required_int(value, "sequence", context=f"sample[{index}].state")
    _required_int(value, "sampled_monotonic_ns", context=f"sample[{index}].state")
    _finite_vector(value.get("position"), field=f"sample[{index}].state.position")
    _finite_vector(value.get("velocity", [0.0] * len(AXES)), field=f"sample[{index}].state.velocity")
    return value


def validate_staging_episode(staging: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    staging = staging.expanduser().resolve()
    if not (staging / "COMPLETE").is_file():
        raise ValueError(f"staging episode is incomplete: {staging / 'COMPLETE'} is missing")
    episode = _load_json(staging / "episode.json")
    samples = _load_samples(staging / "samples.parquet")
    sync_report = _load_json(staging / "sync_report.json")
    timestamp_quality = _load_json(staging / "timestamp_quality.json")

    if episode.get("fps") != FPS:
        raise ValueError(f"episode fps must be {FPS}, got {episode.get('fps')}")
    if episode.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}, got {episode.get('schema_version')!r}")
    if episode.get("action_semantics") != ACTION_SEMANTICS:
        raise ValueError(
            f"action_semantics must be {ACTION_SEMANTICS!r}, got {episode.get('action_semantics')!r}"
        )
    if episode.get("action_source") != "next_state_pseudo_action":
        raise ValueError("action_source must be 'next_state_pseudo_action'")
    identity = episode.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("episode.identity must be an object")
    missing_identity = [field for field in _REQUIRED_IDENTITY_FIELDS if not identity.get(field)]
    if missing_identity:
        raise ValueError(f"episode.identity is missing {missing_identity}")
    commit = str(episode.get("panthera_wam_commit", ""))
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("episode.panthera_wam_commit must be a lowercase 40-character Git SHA")
    calibration_path = staging / "calibration.json"
    if sha256_file(calibration_path) != episode.get("calibration_sha256"):
        raise ValueError("episode.calibration_sha256 does not match calibration.json")

    if int(sync_report.get("canonical_ticks", -1)) != len(samples):
        raise ValueError("sync_report.canonical_ticks does not match samples.parquet")
    if int(sync_report.get("valid_ticks", -1)) != len(samples):
        raise ValueError("sync_report.valid_ticks does not match samples.parquet")
    if int(sync_report.get("timestamp_regressions", -1)) != 0:
        raise ValueError("sync_report.timestamp_regressions must be zero")
    for field in ("missing_frames", "duplicate_frames", "sequence_gaps", "ring_overflows"):
        _require_zero_counts(sync_report.get(field), field=f"sync_report.{field}")
    if not math.isclose(float(timestamp_quality.get("coverage_fraction", 0.0)), 1.0):
        raise ValueError("timestamp_quality.coverage_fraction must be 1.0")
    if int(timestamp_quality.get("timestamp_regressions", -1)) != 0:
        raise ValueError("timestamp_quality.timestamp_regressions must be zero")
    for source in ("state", "overhead_rgb", "wrist_rgb"):
        record = timestamp_quality.get(source)
        if not isinstance(record, dict) or not record.get("source") or not record.get("quality"):
            raise ValueError(f"timestamp_quality.{source} must record source and quality")
        if not math.isclose(float(record.get("coverage_fraction", 0.0)), 1.0):
            raise ValueError(f"timestamp_quality.{source}.coverage_fraction must be 1.0")

    depth_config = episode.get("depth")
    depth_required = isinstance(depth_config, dict) and bool(depth_config.get("requested"))
    camera_keys = ["overhead_rgb", "wrist_rgb"]
    if depth_required:
        if not depth_config.get("complete"):
            raise ValueError("requested depth stream is not complete")
        camera_keys.append("wrist_depth")
        depth_quality = timestamp_quality.get("wrist_depth")
        if not isinstance(depth_quality, dict) or not depth_quality.get("source"):
            raise ValueError("timestamp_quality.wrist_depth is required when depth is requested")

    previous_tick = -1
    previous_state_sequence: int | None = None
    previous_state_timestamp = -1
    previous_camera: dict[str, dict[str, Any]] = {}
    image_shapes: dict[str, tuple[int, int, int]] = {}
    for index, sample in enumerate(samples):
        tick = _required_int(sample, "tick_monotonic_ns", context=f"sample[{index}]")
        if tick <= previous_tick:
            raise ValueError(f"tick_monotonic_ns must be strictly increasing at sample {index}")
        previous_tick = tick

        state = _state(sample, index=index)
        state_sequence = int(state["sequence"])
        state_timestamp = int(state["sampled_monotonic_ns"])
        if previous_state_sequence is not None and state_sequence <= previous_state_sequence:
            raise ValueError(f"state sequence must be strictly increasing at sample {index}")
        if state_timestamp <= previous_state_timestamp:
            raise ValueError(f"state sampled timestamp regressed at sample {index}")
        previous_state_sequence = state_sequence
        previous_state_timestamp = state_timestamp

        sync_ok = sample.get("sync_ok")
        sync_reasons = sample.get("sync_reasons")
        if not isinstance(sync_ok, bool):
            raise ValueError(f"sample[{index}].sync_ok must be boolean")
        if not isinstance(sync_reasons, list):
            raise ValueError(f"sample[{index}].sync_reasons must be a list")
        if not sync_ok or sync_reasons:
            raise ValueError(f"sample[{index}] failed synchronization gates: {sync_reasons}")

        for camera_key in camera_keys:
            camera = _camera(sample, camera_key, index=index)
            sequence = _required_int(camera, "sequence", context=f"sample[{index}].{camera_key}")
            receive_ns = _required_int(
                camera,
                "host_receive_monotonic_ns",
                context=f"sample[{index}].{camera_key}",
            )
            publish_ns = _required_int(
                camera,
                "host_publish_monotonic_ns",
                context=f"sample[{index}].{camera_key}",
            )
            if receive_ns > publish_ns:
                raise ValueError(f"{camera_key} host receive is after publish at sample {index}")
            if not str(camera.get("timestamp_source", "")) or not str(camera.get("timestamp_quality", "")):
                raise ValueError(f"{camera_key} timestamp source/quality missing at sample {index}")
            estimated_ns = camera.get("estimated_capture_monotonic_ns")
            if estimated_ns is not None:
                estimated_ns = _required_int(
                    camera,
                    "estimated_capture_monotonic_ns",
                    context=f"sample[{index}].{camera_key}",
                )
                if int(camera["delta_ns"]) != estimated_ns - tick:
                    raise ValueError(f"{camera_key} delta_ns mismatch at sample {index}")

            previous = previous_camera.get(camera_key)
            if previous is not None:
                if sequence <= previous["sequence"]:
                    raise ValueError(f"{camera_key} sequence must be strictly increasing at sample {index}")
                if receive_ns <= previous["receive_ns"] or publish_ns <= previous["publish_ns"]:
                    raise ValueError(f"{camera_key} host timestamp regression at sample {index}")
                if camera["device_clock_domain"] != previous["device_clock_domain"]:
                    raise ValueError(f"{camera_key} device clock domain changed at sample {index}")
                raw = camera.get("device_timestamp_raw")
                previous_raw = previous["device_timestamp_raw"]
                if raw is not None and previous_raw is not None and float(raw) <= float(previous_raw):
                    raise ValueError(f"{camera_key} device timestamp regression at sample {index}")
                previous_estimated = previous["estimated_ns"]
                if (
                    estimated_ns is not None
                    and previous_estimated is not None
                    and estimated_ns <= previous_estimated
                ):
                    raise ValueError(f"{camera_key} estimated capture regression at sample {index}")
            previous_camera[camera_key] = {
                "sequence": sequence,
                "receive_ns": receive_ns,
                "publish_ns": publish_ns,
                "estimated_ns": estimated_ns,
                "device_clock_domain": camera["device_clock_domain"],
                "device_timestamp_raw": camera.get("device_timestamp_raw"),
            }

            image_path = (staging / str(camera["path"])).resolve()
            if not image_path.is_relative_to(staging) or not image_path.is_file():
                raise ValueError(f"invalid {camera_key} path at sample {index}: {camera['path']}")
            with Image.open(image_path) as image:
                if camera_key == "wrist_depth":
                    if image.mode not in ("I;16", "I"):
                        raise ValueError(f"depth image must be 16-bit, got {image.mode!r}: {image_path}")
                    continue
                shape = (image.height, image.width, 3)
                if image.mode not in ("RGB", "RGBA", "L"):
                    raise ValueError(f"unsupported image mode {image.mode!r}: {image_path}")
            previous_shape = image_shapes.setdefault(camera_key, shape)
            if previous_shape != shape:
                raise ValueError(
                    f"{camera_key} shape changed from {previous_shape} to {shape} at sample {index}"
                )

    return episode, samples


def _feature_spec(image_shapes: dict[str, tuple[int, int, int]]) -> dict[str, dict[str, Any]]:
    axes = list(AXES)

    def scalar(name: str, dtype: str = "int64") -> dict[str, Any]:
        return {"dtype": dtype, "shape": (1,), "names": [name]}

    return {
        "observation.images.overhead_rgb": {
            "dtype": "video",
            "shape": image_shapes["overhead_rgb"],
            "names": ["height", "width", "channels"],
        },
        "observation.images.wrist_rgb": {
            "dtype": "video",
            "shape": image_shapes["wrist_rgb"],
            "names": ["height", "width", "channels"],
        },
        "observation.state": {"dtype": "float32", "shape": (7,), "names": axes},
        "observation.velocity": {"dtype": "float32", "shape": (7,), "names": axes},
        "action": {"dtype": "float32", "shape": (7,), "names": axes},
        "panthera.tick_monotonic_ns": scalar("tick_monotonic_ns"),
        "panthera.state_sampled_monotonic_ns": scalar("state_sampled_monotonic_ns"),
        "panthera.state_sequence": scalar("state_sequence"),
        "panthera.state_interpolated": scalar("state_interpolated", "int8"),
        "panthera.overhead_sequence": scalar("overhead_sequence"),
        "panthera.overhead_host_receive_ns": scalar("overhead_host_receive_ns"),
        "panthera.overhead_host_publish_ns": scalar("overhead_host_publish_ns"),
        "panthera.overhead_estimated_capture_ns": scalar("overhead_estimated_capture_ns"),
        "panthera.overhead_device_timestamp_raw": scalar("overhead_device_timestamp_raw", "float64"),
        "panthera.overhead_delta_ns": scalar("overhead_delta_ns"),
        "panthera.wrist_sequence": scalar("wrist_sequence"),
        "panthera.wrist_host_receive_ns": scalar("wrist_host_receive_ns"),
        "panthera.wrist_host_publish_ns": scalar("wrist_host_publish_ns"),
        "panthera.wrist_estimated_capture_ns": scalar("wrist_estimated_capture_ns"),
        "panthera.wrist_device_timestamp_raw": scalar("wrist_device_timestamp_raw", "float64"),
        "panthera.wrist_delta_ns": scalar("wrist_delta_ns"),
        "panthera.sync_ok": scalar("sync_ok", "int8"),
    }


def _rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _scalar(value: int | float, dtype: str) -> np.ndarray:
    return np.asarray([value], dtype=dtype)


def _sidecar_record(sample: dict[str, Any], next_sample: dict[str, Any], frame_index: int) -> dict[str, Any]:
    return {
        "frame_index": frame_index,
        "tick_monotonic_ns": sample["tick_monotonic_ns"],
        "state": sample["state"],
        "action_source_state_sequence": next_sample["state"]["sequence"],
        "action_source_state_position": next_sample["state"]["position"],
        "action_semantics": ACTION_SEMANTICS,
        "overhead_rgb": sample["overhead_rgb"],
        "wrist_rgb": sample["wrist_rgb"],
        "wrist_depth": sample.get("wrist_depth"),
        "sync_ok": sample["sync_ok"],
    }


def pack_staging_episode(
    staging: Path,
    output: Path,
    *,
    repo_id: str,
    overwrite: bool = False,
    vcodec: str = "h264",
) -> dict[str, Any]:
    from lerobot.datasets.lerobot_dataset import CODEBASE_VERSION, LeRobotDataset

    staging = staging.expanduser().resolve()
    output = output.expanduser().resolve()
    if CODEBASE_VERSION != LEROBOT_CODEBASE_VERSION:
        raise RuntimeError(
            f"LeRobot codebase mismatch: expected {LEROBOT_CODEBASE_VERSION}, got {CODEBASE_VERSION}"
        )
    episode, samples = validate_staging_episode(staging)
    if output.exists():
        if not overwrite:
            raise FileExistsError(output)
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    first_overhead = _camera(samples[0], "overhead_rgb", index=0)
    first_wrist = _camera(samples[0], "wrist_rgb", index=0)
    with Image.open(staging / str(first_overhead["path"])) as image:
        overhead_shape = (image.height, image.width, 3)
    with Image.open(staging / str(first_wrist["path"])) as image:
        wrist_shape = (image.height, image.width, 3)
    features = _feature_spec({"overhead_rgb": overhead_shape, "wrist_rgb": wrist_shape})

    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    sidecar_records: list[dict[str, Any]] = []
    dataset = None
    try:
        dataset = LeRobotDataset.create(
            repo_id=repo_id,
            root=temporary,
            robot_type="panthera_ht_c920e_d405",
            fps=FPS,
            features=features,
            use_videos=True,
            vcodec=vcodec,
            image_writer_threads=2,
        )
        task = str(episode["canonical_task"])
        for frame_index, (sample, next_sample) in enumerate(zip(samples[:-1], samples[1:], strict=True)):
            state = sample["state"]
            overhead = sample["overhead_rgb"]
            wrist = sample["wrist_rgb"]
            frame = {
                "observation.images.overhead_rgb": _rgb(staging / str(overhead["path"])),
                "observation.images.wrist_rgb": _rgb(staging / str(wrist["path"])),
                "observation.state": _finite_vector(state["position"], field="state.position"),
                "observation.velocity": _finite_vector(
                    state.get("velocity", [0.0] * len(AXES)), field="state.velocity"
                ),
                "action": _finite_vector(next_sample["state"]["position"], field="next.state.position"),
                "panthera.tick_monotonic_ns": _scalar(sample["tick_monotonic_ns"], "int64"),
                "panthera.state_sampled_monotonic_ns": _scalar(state["sampled_monotonic_ns"], "int64"),
                "panthera.state_sequence": _scalar(state["sequence"], "int64"),
                "panthera.state_interpolated": _scalar(bool(state.get("interpolated", False)), "int8"),
                "panthera.overhead_sequence": _scalar(overhead["sequence"], "int64"),
                "panthera.overhead_host_receive_ns": _scalar(overhead["host_receive_monotonic_ns"], "int64"),
                "panthera.overhead_host_publish_ns": _scalar(overhead["host_publish_monotonic_ns"], "int64"),
                "panthera.overhead_estimated_capture_ns": _scalar(
                    _optional_int(overhead, "estimated_capture_monotonic_ns"), "int64"
                ),
                "panthera.overhead_device_timestamp_raw": _scalar(
                    _optional_float(overhead, "device_timestamp_raw"), "float64"
                ),
                "panthera.overhead_delta_ns": _scalar(int(overhead["delta_ns"]), "int64"),
                "panthera.wrist_sequence": _scalar(wrist["sequence"], "int64"),
                "panthera.wrist_host_receive_ns": _scalar(wrist["host_receive_monotonic_ns"], "int64"),
                "panthera.wrist_host_publish_ns": _scalar(wrist["host_publish_monotonic_ns"], "int64"),
                "panthera.wrist_estimated_capture_ns": _scalar(
                    _optional_int(wrist, "estimated_capture_monotonic_ns"), "int64"
                ),
                "panthera.wrist_device_timestamp_raw": _scalar(
                    _optional_float(wrist, "device_timestamp_raw"), "float64"
                ),
                "panthera.wrist_delta_ns": _scalar(int(wrist["delta_ns"]), "int64"),
                "panthera.sync_ok": _scalar(bool(sample["sync_ok"]), "int8"),
                "task": task,
            }
            dataset.add_frame(frame)
            sidecar_records.append(_sidecar_record(sample, next_sample, frame_index))
            print(
                json.dumps(
                    {"progress": (frame_index + 1) / (len(samples) - 1), "frame_count": frame_index + 1}
                ),
                flush=True,
            )
        dataset.save_episode(parallel_encoding=False)
        dataset.finalize()
        dataset = None

        aux_dir = temporary / "aux"
        aux_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = aux_dir / "timestamps.jsonl"
        sidecar_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in sidecar_records
            ),
            encoding="utf-8",
        )
        depth_dir = staging / "wrist_depth"
        if depth_dir.is_dir():
            shutil.copytree(depth_dir, aux_dir / "depth")
        source_dir = aux_dir / "source"
        source_dir.mkdir()
        for name in ("sync_report.json", "timestamp_quality.json", "calibration.json"):
            shutil.copy2(staging / name, source_dir / name)
        (temporary / "panthera-episode.json").write_text(
            json.dumps(episode, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        schema = schema_identity()
        schema["identity"] = episode["identity"]
        schema["canonical_task"] = episode["canonical_task"]
        schema["aliases_zh"] = episode.get("aliases_zh", [])
        schema["aliases_en"] = episode.get("aliases_en", [])
        (temporary / "panthera-schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        source_hash = sha256_tree(staging)
        content_hash = sha256_tree(temporary, exclude={"panthera-package-manifest.json"})
        package_manifest = {
            "format": "LeRobotDataset v3.0",
            "lerobot_version": LEROBOT_VERSION,
            "lerobot_codebase_version": LEROBOT_CODEBASE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "schema_sha256": hashlib.sha256(canonical_json_bytes(schema_identity())).hexdigest(),
            "source_staging_sha256": source_hash,
            "dataset_content_sha256": content_hash,
            "frame_count": len(sidecar_records),
            "action_semantics": ACTION_SEMANTICS,
            "source_episode_id": episode["episode_id"],
            "source_panthera_commit": episode["panthera_wam_commit"],
            "source_calibration_sha256": episode["calibration_sha256"],
            "repo_id": repo_id,
        }
        (temporary / "panthera-package-manifest.json").write_text(
            json.dumps(package_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.rename(output)
        return package_manifest
    except BaseException:
        if dataset is not None:
            try:
                dataset.finalize()
            except Exception:
                pass
        shutil.rmtree(temporary, ignore_errors=True)
        raise
