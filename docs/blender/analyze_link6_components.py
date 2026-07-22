"""列出官方 link6 STL 的连通组件包围盒，辅助拆分夹爪。"""

from __future__ import annotations

import json

import bmesh
import bpy
from mathutils import Vector


obj = bpy.data.objects.get("REF_link6")
if obj is None or obj.type != "MESH":
    raise RuntimeError("当前 Blender 文件中缺少 REF_link6")

bm = bmesh.new()
bm.from_mesh(obj.data)
bm.verts.ensure_lookup_table()

remaining = set(bm.verts)
components: list[dict[str, object]] = []
while remaining:
    seed = remaining.pop()
    stack = [seed]
    vertices = {seed}
    while stack:
        vertex = stack.pop()
        for edge in vertex.link_edges:
            other = edge.other_vert(vertex)
            if other in remaining:
                remaining.remove(other)
                vertices.add(other)
                stack.append(other)

    faces = {face for vertex in vertices for face in vertex.link_faces}
    minimum = Vector((float("inf"),) * 3)
    maximum = Vector((float("-inf"),) * 3)
    for vertex in vertices:
        minimum.x = min(minimum.x, vertex.co.x)
        minimum.y = min(minimum.y, vertex.co.y)
        minimum.z = min(minimum.z, vertex.co.z)
        maximum.x = max(maximum.x, vertex.co.x)
        maximum.y = max(maximum.y, vertex.co.y)
        maximum.z = max(maximum.z, vertex.co.z)
    components.append(
        {
            "vertices": len(vertices),
            "faces": len(faces),
            "min": [round(value, 6) for value in minimum],
            "max": [round(value, 6) for value in maximum],
            "center": [round(value, 6) for value in ((minimum + maximum) * 0.5)],
            "size": [round(value, 6) for value in (maximum - minimum)],
        }
    )

bm.free()
components.sort(key=lambda item: int(item["faces"]), reverse=True)
print("LINK6_COMPONENTS=" + json.dumps(components, ensure_ascii=False))
