#!/usr/bin/env bash
# upload-hf.sh — 上传 Panthera 录制数据到 Hugging Face
#
# 目录规范（kv-compression/fastwam-lerobot）：
#   lerobot/<episode>/...          正式采集成品（collectord 原始格式，含 COMPLETE）
#   preview/<task>_<num>/<task>_wrist_<num>.mp4    预演视频（腕部相机）
#   preview/<task>_<num>/<task>_overhead_<num>.mp4 预演视频（顶部相机）
#
# 用法：
#   ./deploy/upload-hf.sh episode <name>            上传单个正式 episode
#   ./deploy/upload-hf.sh episode --all             上传全部 COMPLETE 成品
#   ./deploy/upload-hf.sh preview <video-dir> <task> <num>   上传预演 mp4
#   ./deploy/upload-hf.sh list                      列出仓库已有内容
#
# 选项：
#   --dry-run                只校验与预览，不实际上传
#   --delete-local           上传成功后清理本地 episode（preview 不适用）
#
# 环境变量：
#   HF_REPO_ID              目标仓库（默认 kv-compression/fastwam-lerobot）
#   PANTHERA_DATA_ROOT      episode 根目录（默认 $HOME/panthera-data）
set -euo pipefail

REPO_ID="${HF_REPO_ID:-kv-compression/fastwam-lerobot}"
DATA_ROOT="${PANTHERA_DATA_ROOT:-$HOME/panthera-data}/episodes"
MANIFEST_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/panthera-hf-upload"
HF_TOKEN_FILE="$HOME/.cache/huggingface/token"
DRY_RUN=0
DELETE_LOCAL=0

log() { printf '[upload-hf] %s\n' "$*"; }
die() { printf '[upload-hf] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------- 网络与认证 ----------
# 任意 HTTP 响应码（200/401/403...）都说明网络可达
hf_http_code() {
    local code
    code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null) || code=000
    printf '%s' "$code"
}

ensure_network() {
    local code
    code=$(hf_http_code https://huggingface.co/api/whoami-v2)
    if [ "$code" != "000" ]; then
        log "huggingface.co 直连可用（http_code=$code）"
        return 0
    fi
    log "直连不可用，尝试 sing-box 代理（~/sb）..."
    if [ -f "$HOME/sb/env.sh" ]; then
        # shellcheck disable=SC1091
        source "$HOME/sb/env.sh"
        sbup >/dev/null 2>&1 || true
        proxyon >/dev/null 2>&1 || true
        sleep 2
    fi
    code=$(hf_http_code -x "${http_proxy:-http://127.0.0.1:10808}" https://huggingface.co/api/whoami-v2)
    if [ "$code" != "000" ]; then
        log "代理可用（${http_proxy:-http://127.0.0.1:10808}，http_code=$code）"
        return 0
    fi
    die "无法访问 huggingface.co（直连与代理均失败）"
}

export_hf_token() {
    if [ -n "${HF_TOKEN:-}" ]; then
        return 0
    fi
    if [ -f "$HF_TOKEN_FILE" ]; then
        export HF_TOKEN
        HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
        log "已从 $HF_TOKEN_FILE 读取 HF token"
        return 0
    fi
    die "未找到 HF token（$HF_TOKEN_FILE 或环境变量 HF_TOKEN）"
}

hf_upload() {
    local local_path="$1"
    local remote_path="$2"
    local message="${3:-upload $remote_path}"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] hf upload $REPO_ID $local_path -> $remote_path"
        return 0
    fi
    hf upload --repo-type dataset --quiet --commit-message "$message" \
        "$REPO_ID" "$local_path" "$remote_path"
}

# ---------- 正式 episode ----------
validate_episode() {
    local d="$1"
    [ -f "$d/COMPLETE" ] || die "$d 缺少 COMPLETE 标记"
    [ -f "$d/episode.json" ] || die "$d 缺少 episode.json"
    [ -f "$d/samples.parquet" ] || die "$d 缺少 samples.parquet"
}

upload_episode() {
    local name="$1"
    local dir="$DATA_ROOT/$name"
    [ -d "$dir" ] || die "episode 目录不存在: $dir"
    validate_episode "$dir"
    local manifest="$MANIFEST_DIR/$name.json"
    if [ -f "$manifest" ]; then
        log "$name 已有上传记录，跳过（删除 $manifest 可强制重传）"
        cat "$manifest"
        return 0
    fi
    log "上传正式 episode：$name -> $REPO_ID:lerobot/$name/"
    hf_upload "$dir" "lerobot/$name" "upload episode $name"
    mkdir -p "$MANIFEST_DIR"
    cat >"$manifest" <<EOF
{"episode":"$name","repo_id":"$REPO_ID","remote_path":"lerobot/$name","uploaded_at":"$(date -Iseconds)"}
EOF
    log "完成：$name"
    if [ "$DELETE_LOCAL" -eq 1 ]; then
        log "按 --delete-local 清理本地：$dir"
        rm -rf "$dir"
    fi
}

# ---------- preview ----------
# 规范：preview/<task>_<num>/<task>_wrist_<num>.mp4 + <task>_overhead_<num>.mp4
upload_preview() {
    local dir="$1"
    local task="$2"
    local num="$3"
    [ -d "$dir" ] || die "preview 视频目录不存在: $dir"
    local wrist="$dir/${task}_wrist_${num}.mp4"
    local overhead="$dir/${task}_overhead_${num}.mp4"
    [ -f "$wrist" ] || die "缺少视频 $wrist（先用 deploy/preview-record.sh 录制）"
    [ -f "$overhead" ] || die "缺少视频 $overhead（先用 deploy/preview-record.sh 录制）"
    log "上传预演视频：${task}_${num} -> $REPO_ID:preview/${task}_${num}/"
    hf_upload "$dir" "preview/${task}_${num}" "upload preview ${task}_${num}"
    log "完成：preview/${task}_${num}"
}

# ---------- 仓库列表 ----------
list_repo() {
    ensure_network
    export_hf_token
    curl -fsS -m 30 -H "Authorization: Bearer $HF_TOKEN" \
        "https://huggingface.co/api/datasets/$REPO_ID/tree/main?recursive=true&expand=false" \
        | python3 -c 'import json,sys
d=json.load(sys.stdin)
for x in d:
    p=x.get("path","")
    if x.get("type")=="directory":
        print("dir  "+p+"/")
    else:
        print(f"{x.get("size",0):>12}B  {p}")'
}

# ---------- 入口 ----------
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --delete-local) DELETE_LOCAL=1; shift ;;
        *) break ;;
    esac
done
ensure_network
export_hf_token
cmd="${1:-}"
shift || true
case "$cmd" in
    episode)
        target="${1:-}"
        shift || true
        if [ "$target" = "--all" ]; then
            found=0
            for d in "$DATA_ROOT"/color-block-*; do
                [ -d "$d" ] || continue
                [ -f "$d/COMPLETE" ] || continue
                found=1
                upload_episode "$(basename "$d")"
            done
            [ "$found" -eq 1 ] || die "没有找到 COMPLETE 成品 episode（$DATA_ROOT）"
        else
            [ -n "$target" ] || die "用法: $0 episode <name|--all>"
            upload_episode "$target"
        fi
        ;;
    preview)
        dir="${1:-}"
        shift || true
        task="${1:-}"
        shift || true
        [ -n "$dir" ] && [ -n "$task" ] || die "用法: $0 preview <video-dir> <task> <num>"
        upload_preview "$dir" "$task" "${1:-}"
        ;;
    list)
        list_repo
        ;;
    *)
        die "用法: $0 episode <name|--all> | preview <video-dir> <task> <num> | list"
        ;;
esac
