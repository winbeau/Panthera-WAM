"""Package one complete collectord episode and upload it to a Hugging Face dataset repo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REPO_ID = "winbeau/fastwam-lerobot"
DEFAULT_ENDPOINT = "https://hf-mirror.com"
_REQUIRED_EPISODE_FILES = (
    "COMPLETE",
    "episode.json",
    "samples.parquet",
    "sync_report.json",
    "timestamp_quality.json",
    "calibration.json",
)
_COMMIT_URL = re.compile(r"/commit/([0-9a-f]{40})(?:\b|$)")


class HFEpisodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EpisodeBundle:
    directory: Path
    archive: Path
    checksum: Path
    manifest: Path
    metadata: dict[str, Any]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_episode(path: str | Path) -> tuple[Path, dict[str, Any]]:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise HFEpisodeError("episode root must not be a symlink")
    try:
        episode = raw.resolve(strict=True)
    except OSError as exc:
        raise HFEpisodeError(f"episode directory is unavailable: {raw}") from exc
    if not episode.is_dir():
        raise HFEpisodeError(f"episode path is not a directory: {episode}")
    for name in _REQUIRED_EPISODE_FILES:
        required = episode / name
        if not required.is_file() or required.is_symlink():
            raise HFEpisodeError(f"complete episode is missing a regular {name}: {episode}")
    for item in episode.rglob("*"):
        if item.is_symlink():
            raise HFEpisodeError(f"episode contains a symlink: {item.relative_to(episode)}")
        if not item.is_file() and not item.is_dir():
            raise HFEpisodeError(f"episode contains a non-regular entry: {item.relative_to(episode)}")
    try:
        metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HFEpisodeError("episode.json is not valid UTF-8 JSON") from exc
    if not isinstance(metadata, dict):
        raise HFEpisodeError("episode.json must contain a JSON object")
    episode_id = metadata.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id:
        raise HFEpisodeError("episode.json is missing episode_id")
    if episode_id != episode.name or Path(episode_id).name != episode_id:
        raise HFEpisodeError(
            f"episode_id must equal the directory name: {episode_id!r} != {episode.name!r}"
        )
    return episode, metadata


def _portable_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def prepare_bundle(
    episode_path: str | Path,
    destination: str | Path,
    *,
    kind: str,
) -> EpisodeBundle:
    if kind not in {"episodes", "smoke"}:
        raise HFEpisodeError("kind must be 'episodes' or 'smoke'")
    episode, episode_metadata = validate_episode(episode_path)
    directory = Path(destination).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    episode_id = episode.name
    archive = directory / f"{episode_id}.tar"
    checksum = directory / f"{episode_id}.sha256"
    manifest = directory / f"{episode_id}.json"
    collisions = [path for path in (archive, checksum, manifest) if path.exists()]
    if collisions:
        raise HFEpisodeError(f"bundle output already exists: {collisions[0]}")
    unexpected = next(directory.iterdir(), None)
    if unexpected is not None:
        raise HFEpisodeError(f"bundle directory must be empty: {unexpected}")

    with tarfile.open(archive, mode="w", format=tarfile.PAX_FORMAT) as handle:
        handle.add(
            episode,
            arcname=episode_id,
            recursive=True,
            filter=_portable_tar_info,
        )
    digest = sha256_file(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    bundle_metadata = {
        "schema_version": 1,
        "layout": "panthera-hf-episode-v1",
        "kind": kind,
        "episode_id": episode_id,
        "archive": archive.name,
        "archive_bytes": archive.stat().st_size,
        "sha256": digest,
        "source": "pi5-collectord",
        "source_panthera_commit": episode_metadata.get("panthera_wam_commit", ""),
        "source_calibration_sha256": episode_metadata.get("calibration_sha256", ""),
        "canonical_task": episode_metadata.get("canonical_task", ""),
        "training_data": kind == "episodes",
        "created_wall_time_ns": time.time_ns(),
    }
    manifest.write_text(
        json.dumps(bundle_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return EpisodeBundle(
        directory=directory,
        archive=archive,
        checksum=checksum,
        manifest=manifest,
        metadata=bundle_metadata,
    )


def _extract_revision(output: str) -> str | None:
    match = _COMMIT_URL.search(output)
    return match.group(1) if match else None


def upload_bundle(
    bundle: EpisodeBundle,
    *,
    repo_id: str = DEFAULT_REPO_ID,
    endpoint: str = DEFAULT_ENDPOINT,
    hf_binary: str = "hf",
    commit_message: str | None = None,
    attempts: int = 2,
) -> dict[str, Any]:
    if "/" not in repo_id or repo_id.startswith("/") or repo_id.endswith("/"):
        raise HFEpisodeError("repo_id must have the form owner/dataset")
    if attempts <= 0:
        raise HFEpisodeError("upload attempts must be positive")
    resolved_hf = shutil.which(hf_binary)
    if resolved_hf is None:
        raise HFEpisodeError(f"Hugging Face CLI is not available: {hf_binary}")
    remote_dir = f"staging/{bundle.metadata['kind']}"
    message = commit_message or f"data: upload Pi5 episode {bundle.metadata['episode_id']}"
    command = [
        resolved_hf,
        "upload",
        repo_id,
        str(bundle.directory),
        remote_dir,
        "--repo-type",
        "dataset",
        "--commit-message",
        message,
        "--quiet",
    ]
    environment = os.environ.copy()
    environment["HF_ENDPOINT"] = endpoint
    combined = ""
    for attempt in range(1, attempts + 1):
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        combined = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ).strip()
        if completed.returncode == 0:
            break
        if attempt < attempts:
            time.sleep(min(5.0, float(attempt)))
    else:
        detail = combined[-4000:] if combined else f"exit code {completed.returncode}"
        raise HFEpisodeError(f"Hugging Face upload failed after {attempts} attempts: {detail}")
    return {
        **bundle.metadata,
        "repo_id": repo_id,
        "path_in_repo": remote_dir,
        "endpoint": endpoint,
        "revision": _extract_revision(combined),
        "upload_output": combined,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive one complete collectord episode and upload it to Hugging Face"
    )
    parser.add_argument("episode", type=Path)
    parser.add_argument("--kind", choices=("episodes", "smoke"), default="episodes")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT),
        help="Hugging Face API endpoint; defaults to HF_ENDPOINT or the verified mirror",
    )
    parser.add_argument("--hf-binary", default="hf")
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        help="Keep the prepared archive/checksum/manifest in this directory",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare files without uploading; requires --bundle-dir",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.prepare_only and args.bundle_dir is None:
        raise SystemExit("--prepare-only requires --bundle-dir")
    try:
        if args.bundle_dir is not None:
            bundle = prepare_bundle(args.episode, args.bundle_dir, kind=args.kind)
            result = bundle.metadata if args.prepare_only else upload_bundle(
                bundle,
                repo_id=args.repo_id,
                endpoint=args.endpoint,
                hf_binary=args.hf_binary,
                commit_message=args.commit_message or None,
                attempts=args.attempts,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="panthera-hf-episode-") as temporary:
                bundle = prepare_bundle(args.episode, temporary, kind=args.kind)
                result = upload_bundle(
                    bundle,
                    repo_id=args.repo_id,
                    endpoint=args.endpoint,
                    hf_binary=args.hf_binary,
                    commit_message=args.commit_message or None,
                    attempts=args.attempts,
                )
    except HFEpisodeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
