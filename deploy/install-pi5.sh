#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/panthera-wam
systemd_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
python=${PANTHERA_PYTHON_SYSTEM:-/usr/bin/python3}
bind_address=""
force_config=false

usage() {
    echo "usage: $0 [--bind-address IPV4] [--force-config]" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bind-address)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            bind_address=$2
            shift 2
            ;;
        --force-config)
            force_config=true
            shift
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

case "$(uname -m)" in
    aarch64|arm64) ;;
    *)
        echo "install-pi5.sh requires an ARM64 Linux host" >&2
        exit 1
        ;;
esac

command -v uv >/dev/null 2>&1 || { echo "uv is required" >&2; exit 1; }
[[ -x "$python" ]] || { echo "system Python is missing: $python" >&2; exit 1; }
if git -C "$repo_root" submodule status --recursive | grep -q '^-'; then
    echo "vendor submodules are missing; run: git submodule update --init --recursive" >&2
    exit 1
fi
if [[ ! -f "$repo_root/vendor/Panthera-HT_SDK/panthera_python/robot_param/Follower.yaml" ]]; then
    echo "Panthera SDK vendor submodule is incomplete" >&2
    exit 1
fi

if [[ -z "$bind_address" ]] && command -v tailscale >/dev/null 2>&1; then
    bind_address=$(tailscale ip -4 2>/dev/null | head -n 1 || true)
fi
if [[ -z "$bind_address" ]]; then
    bind_address=$(hostname -I | tr ' ' '\n' | grep -m1 -E '^[0-9]+(\.[0-9]+){3}$' || true)
fi
[[ -n "$bind_address" ]] || { echo "cannot determine an IPv4 bind address" >&2; exit 1; }

python_tag=$(
    "$python" -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")'
)
sdk_wheel=$(
    find "$repo_root/vendor/Panthera-HT_SDK/panthera_python/motor_whl" \
        -maxdepth 1 -type f \
        -name "hightorque_robot-*-cp${python_tag}-cp${python_tag}-linux_aarch64.whl" \
        -print | sort -V | tail -n 1
)
if [[ -z "$sdk_wheel" ]]; then
    echo "no vendored ARM64 SDK wheel matches CPython cp${python_tag}" >&2
    exit 1
fi

uv sync --frozen --all-packages --all-extras --python "$python"
uv pip install --python "$repo_root/.venv/bin/python" --no-deps "$sdk_wheel"
PANTHERA_PYTHON="$repo_root/.venv/bin/python" "$repo_root/deploy/build-realsense-linux.sh"

mkdir -p "$config_dir" "$systemd_dir"
rendered_env=$(mktemp)
trap 'rm -f "$rendered_env"' EXIT
sed \
    -e "s|__PANTHERA_REPO_ROOT__|$repo_root|g" \
    -e "s|__PANTHERA_BIND_ADDRESS__|$bind_address|g" \
    "$repo_root/deploy/armd.pi5.env.example" > "$rendered_env"
if [[ ! -f "$config_dir/armd.env" ]] || $force_config; then
    install -m 0600 "$rendered_env" "$config_dir/armd.env"
else
    echo "preserving existing $config_dir/armd.env (use --force-config to replace it)"
fi

escaped_repo_root=${repo_root//&/\\&}
sed "s|@REPO_ROOT@|$escaped_repo_root|g" \
    "$repo_root/deploy/armd.service.in" > "$systemd_dir/armd.service"
sed "s|@REPO_ROOT@|$escaped_repo_root|g" \
    "$repo_root/deploy/camerad.service.in" > "$systemd_dir/camerad.service"
systemctl --user daemon-reload
systemctl --user enable camerad.service armd.service

uv run --no-sync --package panthera-armd armd --sim --check
uv run --no-sync --package panthera-armd camerad --mode sim --check

echo "Raspberry Pi 5 deployment installed and simulation checks passed."
echo "WPF arm endpoint:    http://$bind_address:50051"
echo "WPF camera endpoint: http://$bind_address:50052"
echo "Services were enabled but not started; real hardware remains untouched."
echo "Install udev rules once, then ask the operator for explicit confirmation before starting armd:"
echo "  sudo install -m 0644 '$repo_root/deploy/99-panthera-ht.rules' /etc/udev/rules.d/"
echo "  sudo install -m 0644 '$repo_root/vendor/librealsense/config/99-realsense-libusb.rules' /etc/udev/rules.d/"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"
