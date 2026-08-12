from __future__ import annotations

import argparse
import json
from pathlib import Path

from .packager import pack_staging_episode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pack a Panthera staging episode as LeRobotDataset v3")
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--vcodec", default="h264")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    manifest = pack_staging_episode(
        args.staging,
        args.output,
        repo_id=args.repo_id,
        overwrite=args.overwrite,
        vcodec=args.vcodec,
    )
    print(json.dumps({"progress": 1.0, **manifest}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
