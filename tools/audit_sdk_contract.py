#!/usr/bin/env python3
"""验证 42 项 SDK 能力在源码、RPC、CLI 和内部覆盖之间形成闭环。"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path

from panthera_arm import arm_pb2
from typer.main import get_command

from armd.grpc_service import ArmService
from panthera_cli.__main__ import app

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "docs" / "sdk-capability-audit.json"
SDK_CLASSES = {
    "Panthera": (
        ROOT / "vendor/Panthera-HT_SDK/panthera_python/scripts/Panthera_lib/Panthera.py",
        "Panthera",
    ),
    "Recorder": (
        ROOT / "vendor/Panthera-HT_SDK/panthera_python/scripts/Panthera_lib/recorder.py",
        "Recorder",
    ),
}


def class_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    return {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}


def command_paths(group, prefix: tuple[str, ...] = ()) -> set[str]:
    result: set[str] = set()
    for name, command in group.commands.items():
        path = (*prefix, name)
        if hasattr(command, "commands"):
            result.update(command_paths(command, path))
        else:
            result.add(" ".join(path))
    return result


def resolve_symbol(value: str):
    module_name, path = value.split(":", 1)
    current = importlib.import_module(module_name)
    for component in path.split("."):
        current = getattr(current, component)
    return current


def main() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    capabilities = audit["capabilities"]
    errors: list[str] = []

    ids = [item["id"] for item in capabilities]
    if ids != list(range(1, 43)):
        errors.append(f"能力 ID 必须严格为 1..42，当前为 {ids}")
    pairs = [(item["owner"], item["method"]) for item in capabilities]
    if len(set(pairs)) != 42:
        errors.append("owner/method 存在重复")

    sdk_methods = {
        owner: class_methods(path, class_name) for owner, (path, class_name) in SDK_CLASSES.items()
    }
    rpc_methods = {method.name for method in arm_pb2.DESCRIPTOR.services_by_name["ArmService"].methods}
    implemented_rpcs = set(ArmService.__dict__)
    cli_paths = command_paths(get_command(app))

    for item in capabilities:
        label = f"#{item['id']} {item['owner']}.{item['method']}"
        if item["method"] not in sdk_methods[item["owner"]]:
            errors.append(f"{label}: SDK 源码中不存在")
        if item["disposition"] == "direct" and (not item["cli"] or not item["rpc"]):
            errors.append(f"{label}: direct 条目必须同时有 CLI 和 RPC")
        if item["disposition"] != "direct" and not item.get("rationale"):
            errors.append(f"{label}: 非 direct 条目缺少 rationale")
        for rpc in item["rpc"]:
            if rpc not in rpc_methods:
                errors.append(f"{label}: arm.proto 缺少 RPC {rpc}")
            elif rpc not in implemented_rpcs:
                errors.append(f"{label}: ArmService 未实现 RPC {rpc}")
        for cli in item["cli"]:
            if cli not in cli_paths:
                errors.append(f"{label}: CLI 命令不存在 {cli}")
        implementation = item.get("implementation")
        if implementation:
            try:
                symbol = resolve_symbol(implementation)
            except (ImportError, AttributeError, ValueError) as exc:
                errors.append(f"{label}: implementation 无法解析 {implementation}: {exc}")
            else:
                if not (callable(symbol) or inspect.isclass(symbol)):
                    errors.append(f"{label}: implementation 不是可调用符号 {implementation}")

    bindings = (ROOT / "vendor/Panthera-HT_SDK/panthera_python/src/bindings.cpp").read_text(encoding="utf-8")
    real_backend = (ROOT / "armd/src/armd/backend/real.py").read_text(encoding="utf-8")
    inherited_checks = {
        "set_stop(): void": '.def("set_stop", &robot::set_stop' in bindings,
        "set_reset_zero(): void": "static_cast<void (robot::*)()>(&robot::set_reset_zero)" in bindings,
        "set_reset_zero_motors(list): void": (
            "static_cast<void (robot::*)(std::initializer_list<int>)>(&robot::set_reset_zero)" in bindings
        ),
        "RPC 1..7 转 SDK 0..6": "[motor_id - 1 for motor_id in ids]" in real_backend,
    }
    for label, passed in inherited_checks.items():
        if not passed:
            errors.append(f"继承层签名检查失败: {label}")

    dispositions: dict[str, int] = {}
    for item in capabilities:
        dispositions[item["disposition"]] = dispositions.get(item["disposition"], 0) + 1
    result = {
        "ok": not errors,
        "total": len(capabilities),
        "dispositions": dispositions,
        "rpc_count": len(rpc_methods),
        "cli_command_count": len(cli_paths),
        "inherited_checks": inherited_checks,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
