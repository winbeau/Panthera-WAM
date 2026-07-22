"""Build the browser mesh bundle used by the Panthera three-view mockup.

The source geometry is the official follower URDF/STL set pinned in the SDK
submodule.  The generated asset keeps the real CAD silhouette while reducing
triangle count enough for three simultaneous orthographic WebGL views.

Requirements (kept outside the application dependency graph):
    python -m pip install trimesh fast-simplification networkx
"""

from __future__ import annotations

import base64
import gzip
import struct
from pathlib import Path

import numpy as np
import trimesh


ROOT = Path(__file__).resolve().parents[3]
MESH_DIR = (
    ROOT
    / "vendor"
    / "Panthera-HT_SDK"
    / "panthera_python"
    / "Panthera-HT_description"
    / "meshes"
)
OUTPUT = ROOT / "docs" / "mockups" / "assets" / "panthera-ht-meshes.min.js"

# Large structural links keep more faces because their ventilation slots,
# gussets, motor housings, and fasteners define the reference side silhouette.
TARGET_FACES = {
    "base_link": 25_000,
    "link1": 6_000,
    "link2": 50_000,
    "link3": 45_000,
    "link4": 15_000,
    "link5": 15_000,
    "link6_body": 18_000,
    "gripper_left": 9_000,
    "gripper_right": 9_000,
}


def reduce_mesh(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
    mesh.remove_unreferenced_vertices()
    return mesh


def load_and_reduce(name: str, target_faces: int) -> trimesh.Trimesh:
    source = MESH_DIR / f"{name}.STL"
    if not source.exists():
        raise FileNotFoundError(
            f"Missing {source}. Initialize the SDK submodule with "
            "`git submodule update --init vendor/Panthera-HT_SDK`."
        )

    mesh = trimesh.load_mesh(source, process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise TypeError(f"Expected one triangle mesh in {source}")

    return reduce_mesh(mesh, target_faces)


def load_link6_parts() -> dict[str, trimesh.Trimesh]:
    source = MESH_DIR / "link6.STL"
    mesh = trimesh.load_mesh(source, process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise TypeError(f"Expected one triangle mesh in {source}")

    grouped: dict[str, list[trimesh.Trimesh]] = {
        "link6_body": [],
        "gripper_left": [],
        "gripper_right": [],
    }
    for component in mesh.split(only_watertight=False):
        minimum, maximum = component.bounds
        if maximum[0] > 0.055 and minimum[1] >= 0.040:
            grouped["gripper_left"].append(component)
        elif maximum[0] > 0.055 and maximum[1] <= -0.040:
            grouped["gripper_right"].append(component)
        else:
            grouped["link6_body"].append(component)

    result: dict[str, trimesh.Trimesh] = {}
    for name, components in grouped.items():
        if not components:
            raise RuntimeError(f"link6 split produced no components for {name}")
        combined = trimesh.util.concatenate(components)
        result[name] = reduce_mesh(combined, TARGET_FACES[name])
    return result


def pack_mesh(name: str, mesh: trimesh.Trimesh) -> bytes:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.uint32).reshape(-1)
    if len(vertices) > np.iinfo(np.uint16).max:
        raise ValueError(f"{name} still has too many vertices for uint16 indices")

    minimum = vertices.min(axis=0)
    extent = vertices.max(axis=0) - minimum
    extent[extent == 0] = 1.0
    quantized = np.rint((vertices - minimum) / extent * 65535.0).astype("<u2")
    indices = faces.astype("<u2")
    encoded_name = name.encode("ascii")

    return b"".join(
        [
            struct.pack("<B", len(encoded_name)),
            encoded_name,
            struct.pack("<II", len(vertices), len(indices)),
            struct.pack("<6f", *minimum.astype(np.float32), *extent.astype(np.float32)),
            quantized.tobytes(order="C"),
            indices.tobytes(order="C"),
        ]
    )


def main() -> None:
    payload = [b"PHTV1\0\0\0", struct.pack("<H", len(TARGET_FACES))]
    summaries: list[str] = []

    meshes = {
        name: load_and_reduce(name, target_faces)
        for name, target_faces in TARGET_FACES.items()
        if not name.startswith("gripper_") and name != "link6_body"
    }
    meshes.update(load_link6_parts())

    for name, target_faces in TARGET_FACES.items():
        mesh = meshes[name]
        payload.append(pack_mesh(name, mesh))
        summaries.append(f"{name}:{len(mesh.vertices)}v/{len(mesh.faces)}f")

    compressed = gzip.compress(b"".join(payload), compresslevel=9, mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "// Generated from the pinned official Panthera-HT follower STL set.\n"
        "// Regenerate with docs/mockups/tools/build-panthera-three-view-meshes.py.\n"
        f"window.PANTHERA_HT_MESH_SUMMARY = {summaries!r};\n"
        f"window.PANTHERA_HT_MESH_B64 = \"{encoded}\";\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size:,} bytes)")
    print("; ".join(summaries))


if __name__ == "__main__":
    main()
