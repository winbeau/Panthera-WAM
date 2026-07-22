#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$repo_root/vendor/librealsense"
build_dir=${PANTHERA_REALSENSE_BUILD_DIR:-"$repo_root/build/realsense-rsusb"}
python=${PANTHERA_PYTHON:-"$repo_root/.venv/bin/python"}
jobs=${PANTHERA_BUILD_JOBS:-$(nproc)}

if [[ ! -f "$source_dir/CMakeLists.txt" ]]; then
    echo "librealsense submodule is missing; run: git submodule update --init --recursive" >&2
    exit 1
fi
if [[ ! -x "$python" ]]; then
    echo "Python environment is missing; run: uv sync --all-packages --all-extras" >&2
    exit 1
fi
if ! command -v cmake >/dev/null 2>&1 || ! command -v pkg-config >/dev/null 2>&1; then
    echo "cmake/pkg-config is missing; install the Linux build dependencies first" >&2
    exit 1
fi
if ! pkg-config --exists libusb-1.0; then
    echo "libusb development files are missing; run:" >&2
    echo "  sudo apt-get install -y build-essential cmake libssl-dev libusb-1.0-0-dev pkg-config" >&2
    exit 1
fi

cmake -S "$source_dir" -B "$build_dir" \
    -DFORCE_RSUSB_BACKEND=ON \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DPYTHON_EXECUTABLE="$python" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_GRAPHICAL_EXAMPLES=OFF \
    -DBUILD_GLSL_EXTENSIONS=OFF \
    -DBUILD_UNIT_TESTS=OFF \
    -DBUILD_TOOLS=OFF \
    -DCHECK_FOR_UPDATES=OFF \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_dir" --target pyrealsense2 -j"$jobs"

extension=$(find -L "$build_dir/Release" -maxdepth 1 -type f -name 'pyrealsense2*.so' -print -quit)
if [[ -z "$extension" ]]; then
    echo "pyrealsense2 build output was not found under $build_dir/Release" >&2
    exit 1
fi

site_packages=$(
    "$python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
)
package_dir="$site_packages/pyrealsense2"
install -d "$package_dir"
install -m 0644 "$source_dir/wrappers/python/pyrealsense2/__init__.py" "$package_dir/__init__.py"
install -m 0755 "$extension" "$package_dir/$(basename "$extension")"

"$python" - <<'PY'
import pyrealsense2 as rs
from pyrealsense2 import pyrealsense2 as native

print(f"vendored pyrealsense2 {native.__version__}: {rs.__file__}")
PY
