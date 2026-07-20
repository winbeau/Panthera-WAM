# Panthera-WAM 真机后端恢复命令。
# 机械臂与 D405 在同一 WSL 内分进程隔离，由一个命令统一启动。
# armd:50051 与 camerad:50052 分别提供机械臂和相机 gRPC 服务。

panthera-up() {
    emulate -L zsh
    setopt pipefail
    unsetopt bg_nice

    local repo="${PANTHERA_REPO:-$HOME/Panthera-WAM-v2}"
    local sdk="$repo/vendor/Panthera-HT_SDK"
    local config="$sdk/panthera_python/robot_param/Follower.yaml"
    local motor_wheel="$sdk/panthera_python/motor_whl/hightorque_robot-1.2.0-cp311-cp311-linux_x86_64.whl"
    local state_dir="$HOME/.local/state/panthera"
    local log="$state_dir/armd.log"
    local camera_log="$state_dir/camerad.log"
    local public_bind="127.0.0.1:50051"
    local endpoint="[::1]:50051"
    local camera_public_bind="127.0.0.1:50052"
    local camera_endpoint="[::1]:50052"
    local python="$repo/.venv/bin/python"
    local armd="$repo/.venv/bin/armd"
    local camerad="$repo/.venv/bin/camerad"
    local cli="$repo/.venv/bin/panthera"
    local uv_bin=""
    local pyver=""
    local pid=""
    local camera_pid=""
    local daemon_status=""
    local camera_status=""
    local armd_entry_pattern='[.]venv/bin/[a]rmd'
    local armd_module_pattern="$repo/[.]venv/bin/python -m [a]rmd"
    local camerad_entry_pattern='[.]venv/bin/[c]amerad'
    local camerad_module_pattern="$repo/[.]venv/bin/python -m [a]rmd.camera"
    local attempt
    local -a devices

    uv_bin=$(command -v uv 2>/dev/null)
    [[ -n "$uv_bin" ]] || uv_bin="$HOME/.local/bin/uv"

    if [[ ! -f "$repo/pyproject.toml" || ! -f "$config" || ! -x "$uv_bin" ]]; then
        print -u2 "Panthera 项目、SDK 或 uv 不完整："
        print -u2 "  repo:   $repo"
        print -u2 "  config: $config"
        print -u2 "  uv:     $uv_bin"
        return 1
    fi

    print "[1/7] 等待机械臂 USB 挂载到 WSL..."
    for attempt in {1..20}; do
        devices=(/dev/ttyACM*(N))
        (( ${#devices} >= 4 )) && break
        sleep 0.5
    done
    if (( ${#devices} < 4 )); then
        print -u2 "只发现 ${#devices} 路 ttyACM，真实机械臂后端未启动。"
        print -u2 "请在 Windows PowerShell 执行统一动态连接："
        print -u2 "  ./deploy/attach-wsl-usb.ps1"
        return 2
    fi
    print "      已发现 ${#devices} 路串口：${devices[*]}"

    print "[2/7] 检查 Intel RealSense D405..."
    for attempt in {1..20}; do
        command lsusb -d 8086:0b5b >/dev/null 2>&1 && break
        sleep 0.5
    done
    if ! command lsusb -d 8086:0b5b >/dev/null 2>&1; then
        print -u2 "WSL 中未发现 D405 (8086:0b5b)。"
        print -u2 "请在 Windows PowerShell 执行统一动态连接："
        print -u2 "  ./deploy/attach-wsl-usb.ps1"
        return 2
    fi
    print "      D405 已挂载到 WSL。"

    if [[ -x "$python" ]]; then
        pyver=$($python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
    fi
    if [[ "$pyver" != "3.11" || ! -x "$armd" || ! -x "$camerad" || ! -x "$cli" ]]; then
        print "[3/7] 构建 Python 3.11 工作区..."
        (cd "$repo" && command "$uv_bin" sync --frozen --all-packages --all-extras --python 3.11) || return 3
    else
        print "[3/7] Python 3.11 环境正常。"
    fi

    if ! "$python" -c 'import hightorque_robot' >/dev/null 2>&1; then
        if [[ ! -f "$motor_wheel" ]]; then
            print -u2 "官方电机 SDK wheel 不存在：$motor_wheel"
            return 3
        fi
        print "      安装官方 hightorque_robot 1.2.0 wheel..."
        command env UV_SKIP_WHEEL_FILENAME_CHECK=1 "$uv_bin" pip install \
            --python "$python" "$motor_wheel" || return 3
    fi

    print "[4/7] 检查 vendored librealsense RSUSB 后端..."
    if ! "$python" -c 'from pyrealsense2 import pyrealsense2 as rs; assert rs.__version__ == "2.58.1"' \
        >/dev/null 2>&1; then
        print "      编译并安装 D405 Python 绑定（首次需数分钟）..."
        (cd "$repo" && ./deploy/build-realsense-wsl.sh) || return 3
    fi
    print "      librealsense 2.58.1 RSUSB 已就绪。"

    # 两个后端只访问本机 gRPC；函数返回后恢复调用者环境。
    local http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
    local grpc_proxy GRPC_PROXY
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy grpc_proxy GRPC_PROXY
    local -x NO_PROXY="127.0.0.1,localhost,::1"
    local -x no_proxy="127.0.0.1,localhost,::1"

    print "[5/7] 清理旧 armd/camerad/仿真进程..."
    command pkill -INT -f "$armd_entry_pattern" 2>/dev/null || true
    command pkill -INT -f "$armd_module_pattern" 2>/dev/null || true
    command pkill -INT -f "$camerad_entry_pattern" 2>/dev/null || true
    command pkill -INT -f "$camerad_module_pattern" 2>/dev/null || true
    for attempt in {1..60}; do
        if ! command pgrep -f "$armd_entry_pattern" >/dev/null 2>&1 \
            && ! command pgrep -f "$armd_module_pattern" >/dev/null 2>&1 \
            && ! command pgrep -f "$camerad_entry_pattern" >/dev/null 2>&1 \
            && ! command pgrep -f "$camerad_module_pattern" >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done
    if command pgrep -f "$armd_entry_pattern" >/dev/null 2>&1 \
        || command pgrep -f "$armd_module_pattern" >/dev/null 2>&1 \
        || command pgrep -f "$camerad_entry_pattern" >/dev/null 2>&1 \
        || command pgrep -f "$camerad_module_pattern" >/dev/null 2>&1; then
        print "      旧后端未响应 SIGINT，发送 SIGTERM..."
        command pkill -TERM -f "$armd_entry_pattern" 2>/dev/null || true
        command pkill -TERM -f "$armd_module_pattern" 2>/dev/null || true
        command pkill -TERM -f "$camerad_entry_pattern" 2>/dev/null || true
        command pkill -TERM -f "$camerad_module_pattern" 2>/dev/null || true
        for attempt in {1..20}; do
            if ! command pgrep -f "$armd_entry_pattern" >/dev/null 2>&1 \
                && ! command pgrep -f "$armd_module_pattern" >/dev/null 2>&1 \
                && ! command pgrep -f "$camerad_entry_pattern" >/dev/null 2>&1 \
                && ! command pgrep -f "$camerad_module_pattern" >/dev/null 2>&1; then
                break
            fi
            sleep 0.25
        done
    fi
    if command ss -ltn | command grep -qE ':50051[[:space:]]'; then
        print -u2 "端口 50051 仍被其他进程占用，请运行：ss -ltnp | grep 50051"
        return 4
    fi
    if command ss -ltn | command grep -qE ':50052[[:space:]]'; then
        print -u2 "端口 50052 仍被其他进程占用，请运行：ss -ltnp | grep 50052"
        return 4
    fi

    print "[6/7] 启动 WSL camerad（D405 RSUSB）..."
    command mkdir -p "$state_dir"
    : >| "$camera_log"
    nohup env \
        PYTHONUNBUFFERED=1 \
        PANTHERA_CAMERA_MODE=auto \
        "$camerad" \
        --mode auto \
        --bind "$camera_public_bind" \
        --local-bind "$camera_endpoint" \
        >>"$camera_log" 2>&1 </dev/null &!
    camera_pid=$!

    for attempt in {1..80}; do
        if command ss -ltn | command grep -qE ':50052[[:space:]]'; then
            camera_status=$(command timeout 2s env \
                NO_COLOR=1 PANTHERA_CAMERA_ENDPOINT="$camera_endpoint" \
                "$cli" camera status --json 2>/dev/null) || camera_status=""
            break
        fi
        if ! command kill -0 "$camera_pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    if ! command ss -ltn | command grep -qE ':50052[[:space:]]'; then
        print -u2 "camerad 启动失败，最近日志："
        command tail -60 "$camera_log" >&2
        command kill -INT "$camera_pid" 2>/dev/null || true
        return 5
    fi
    if [[ -z "$camera_status" ]]; then
        print -u2 "      警告：camerad 已监听，但 D405 状态仍在初始化；继续启动机械臂后端。"
    elif [[ "$camera_status" == *'"available": false'* ]]; then
        print -u2 "      警告：camerad 已启动，但 D405 暂不可用；继续启动机械臂后端。"
    fi

    print "[7/7] 启动机械臂 armd（150ms 固件看门狗）..."
    : >| "$log"
    nohup env \
        PYTHONUNBUFFERED=1 \
        PANTHERA_SDK_ROOT="$sdk" \
        PANTHERA_CONFIG="$config" \
        PANTHERA_MOTOR_TIMEOUT_MS=150 \
        PANTHERA_CAMERA_MODE=off \
        "$armd" \
        --bind "$public_bind" \
        --local-bind "$endpoint" \
        --sdk-root "$sdk" \
        --config "$config" \
        --motor-timeout-ms 150 \
        --camera-mode off \
        >>"$log" 2>&1 </dev/null &!
    pid=$!

    for attempt in {1..80}; do
        if ! command kill -0 "$pid" 2>/dev/null; then
            break
        fi
        if command ss -ltn | command grep -qE ':50051[[:space:]]'; then
            if daemon_status=$(command timeout 3s env \
                NO_COLOR=1 PANTHERA_ENDPOINT="$endpoint" \
                "$cli" daemon status --json 2>/dev/null); then
                camera_status=$(command timeout 2s env \
                    NO_COLOR=1 PANTHERA_CAMERA_ENDPOINT="$camera_endpoint" \
                    "$cli" camera status --json 2>/dev/null) || camera_status=""
                print "Panthera 统一后端已恢复：armd PID=$pid，camerad PID=$camera_pid"
                print "  机械臂：localhost:50051；WSL 探活：$endpoint"
                if [[ -n "$camera_status" ]]; then
                    print "  D405：  localhost:50052；WSL 探活：$camera_endpoint"
                else
                    print -u2 "  D405：  camerad 已监听，状态仍在初始化（不影响机械臂控制）"
                fi
                export PANTHERA_ENDPOINT="$endpoint"
                export PANTHERA_CAMERA_ENDPOINT="$camera_endpoint"
                return 0
            fi
        fi
        sleep 0.5
    done

    print -u2 "armd 启动失败，最近日志："
    command tail -60 "$log" >&2
    command kill -INT "$pid" 2>/dev/null || true
    command kill -INT "$camera_pid" 2>/dev/null || true
    return 5
}
