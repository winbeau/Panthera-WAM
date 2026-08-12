from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from armd.hf_episode import (
    HFEpisodeError,
    prepare_bundle,
    sha256_file,
    upload_bundle,
    validate_episode,
)


def _episode(root: Path, *, episode_id: str = "episode-000001") -> Path:
    episode = root / episode_id
    episode.mkdir(parents=True)
    (episode / "COMPLETE").write_text("complete\n", encoding="utf-8")
    (episode / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": episode_id,
                "canonical_task": "Move the red block.",
                "panthera_wam_commit": "a" * 40,
                "calibration_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "samples.parquet",
        "sync_report.json",
        "timestamp_quality.json",
        "calibration.json",
    ):
        (episode / name).write_bytes(name.encode())
    (episode / "overhead").mkdir()
    (episode / "overhead/000000.jpg").write_bytes(b"jpeg")
    return episode


def test_prepare_bundle_writes_portable_archive_checksum_and_manifest(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "source")
    bundle = prepare_bundle(episode, tmp_path / "bundle", kind="episodes")

    assert bundle.archive.name == "episode-000001.tar"
    assert bundle.checksum.read_text() == f"{sha256_file(bundle.archive)}  {bundle.archive.name}\n"
    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["episode_id"] == "episode-000001"
    assert manifest["training_data"] is True
    assert manifest["sha256"] == sha256_file(bundle.archive)
    with tarfile.open(bundle.archive) as archive:
        names = set(archive.getnames())
        assert "episode-000001/COMPLETE" in names
        assert "episode-000001/episode.json" in names
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)


def test_validate_episode_requires_complete_identity_and_no_symlinks(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "source")
    assert validate_episode(episode)[0] == episode.resolve()

    (episode / "COMPLETE").unlink()
    with pytest.raises(HFEpisodeError, match="COMPLETE"):
        validate_episode(episode)

    episode = _episode(tmp_path / "second", episode_id="different")
    metadata = json.loads((episode / "episode.json").read_text())
    metadata["episode_id"] = "wrong"
    (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(HFEpisodeError, match="directory name"):
        validate_episode(episode)

    episode = _episode(tmp_path / "third", episode_id="symlinked")
    (episode / "bad-link").symlink_to(episode / "COMPLETE")
    with pytest.raises(HFEpisodeError, match="symlink"):
        validate_episode(episode)


def test_prepare_bundle_rejects_overwrite_and_invalid_kind(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "source")
    prepare_bundle(episode, tmp_path / "bundle", kind="smoke")
    with pytest.raises(HFEpisodeError, match="already exists"):
        prepare_bundle(episode, tmp_path / "bundle", kind="smoke")
    with pytest.raises(HFEpisodeError, match="kind"):
        prepare_bundle(episode, tmp_path / "other", kind="bad")


def test_upload_bundle_uses_dataset_repo_mirror_and_reports_revision(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "source")
    bundle = prepare_bundle(episode, tmp_path / "bundle", kind="episodes")
    log = tmp_path / "hf-log.json"
    fake_hf = tmp_path / "hf"
    fake_hf.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps({{'args': sys.argv[1:], 'endpoint': os.environ.get('HF_ENDPOINT')}}))\n"
        "print('https://huggingface.co/datasets/winbeau/fastwam-lerobot/commit/' + 'c' * 40)\n",
        encoding="utf-8",
    )
    fake_hf.chmod(0o755)

    result = upload_bundle(
        bundle,
        hf_binary=str(fake_hf),
        endpoint="https://hf-mirror.example",
        attempts=1,
    )

    invocation = json.loads(log.read_text())
    assert invocation["endpoint"] == "https://hf-mirror.example"
    assert invocation["args"][:2] == ["upload", "winbeau/fastwam-lerobot"]
    assert invocation["args"][2:4] == [str(bundle.directory), "staging/episodes"]
    assert "--repo-type" in invocation["args"]
    assert result["revision"] == "c" * 40
    assert result["path_in_repo"] == "staging/episodes"
