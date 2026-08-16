#!/usr/bin/env bash
# 简单封装：重启 armd 服务并报告状态。
#
# ⚠ 高位（非初始0位）状态下重启有坠臂风险（固件 150ms 看门狗），
#   执行前请先 zero-home 回低位，或至少有人扶住承重关节。
set -uo pipefail

echo "==> systemctl --user restart armd.service"
systemctl --user restart armd.service
sleep 8

if systemctl --user is-active armd.service >/dev/null 2>&1; then
    echo "✅ armd 服务正常（active）"
else
    echo "❌ armd 服务未激活" >&2
    exit 1
fi
