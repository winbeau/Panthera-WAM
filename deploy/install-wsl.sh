#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/panthera-wam
systemd_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user
start_service=false

if [[ ${1:-} == "--start" ]]; then
    start_service=true
elif [[ $# -gt 0 ]]; then
    echo "usage: $0 [--start]" >&2
    exit 2
fi

mkdir -p "$config_dir" "$systemd_dir"
if [[ ! -f "$config_dir/armd.env" ]]; then
    sed "s|/home/<wsl-user>/panthera-wam|$repo_root|g" \
        "$repo_root/deploy/armd.env.example" > "$config_dir/armd.env"
    chmod 600 "$config_dir/armd.env"
fi

escaped_repo_root=${repo_root//&/\\&}
sed "s|@REPO_ROOT@|$escaped_repo_root|g" \
    "$repo_root/deploy/armd.service.in" > "$systemd_dir/armd.service"

systemctl --user daemon-reload
systemctl --user enable armd.service

echo "armd user service installed: $systemd_dir/armd.service"
echo "environment file: $config_dir/armd.env"
echo "install the serial rule once with:"
echo "  sudo install -m 0644 '$repo_root/deploy/99-panthera-ht.rules' /etc/udev/rules.d/"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"

if $start_service; then
    systemctl --user restart armd.service
    systemctl --user --no-pager --full status armd.service
else
    echo "service was not started; use: systemctl --user start armd"
fi
