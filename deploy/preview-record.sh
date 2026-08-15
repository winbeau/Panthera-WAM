#!/usr/bin/env bash
# 连续录制两路 preview 视频，不采集关节/深度，不占用 teach lease。
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$repo_root/.venv/bin/python" "$repo_root/tools/preview-record.py" "$@"
