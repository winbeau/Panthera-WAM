#!/usr/bin/env bash
# 从 proto/arm.proto 与 proto/camera.proto 生成两端 stub。
#
# CLAUDE.md 开发约定：改动 arm.proto 后必须重新生成 Python 与 C# stub，两端一起提交。
#
#   Python : 生成到 proto/gen/python/panthera_arm/（供 armd 与 cli 以 uv path 依赖引用）
#   C#     : 不在此脚本生成。WPF 侧由 Grpc.Tools 在 dotnet build 时按 csproj 里的
#            <Protobuf Include="..\..\proto\arm.proto" /> 自动生成，避免生成物入库两份。
#
# 用法： ./proto/gen.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/proto/gen/python/panthera_arm"
mkdir -p "$OUT"

echo "==> 生成 Python stub → $OUT"
uv run --with grpcio-tools==1.82.1 python -m grpc_tools.protoc \
    -I "$ROOT/proto" \
    --python_out="$OUT" \
    --pyi_out="$OUT" \
    --grpc_python_out="$OUT" \
    "$ROOT/proto/arm.proto" \
    "$ROOT/proto/camera.proto"

# grpc_tools 生成的 arm_pb2_grpc.py 里是顶层 `import arm_pb2`，
# 放进包内会 ImportError；改成包内相对导入。
for generated_grpc in "$OUT"/*_pb2_grpc.py; do
    module=$(basename "$generated_grpc" _pb2_grpc.py)
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s/^import ${module}_pb2 as/from . import ${module}_pb2 as/" "$generated_grpc"
    else
        sed -i "s/^import ${module}_pb2 as/from . import ${module}_pb2 as/" "$generated_grpc"
    fi
done

cat > "$OUT/__init__.py" <<'PYEOF'
"""由 proto/*.proto 生成的 gRPC stub —— 请勿手工编辑。

重新生成： ./proto/gen.sh
"""
from . import arm_pb2, arm_pb2_grpc, camera_pb2, camera_pb2_grpc

__all__ = ["arm_pb2", "arm_pb2_grpc", "camera_pb2", "camera_pb2_grpc"]
PYEOF

echo "==> 完成："
ls -1 "$OUT"
