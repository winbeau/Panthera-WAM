"""Panthera-HT 三视图手调场景生成器。

在 Blender 的 Scripting 工作区打开并运行本文件。脚本会：

1. 按官方 follower URDF 建立 J1-J6 父子层级和角度滑块；
2. 导入 vendor 中的官方 STL，作为可切换的线框参考；
3. 建立一套可直接缩放、进 Edit Mode 改顶点的简化设计外壳；
4. 创建侧视、主视、俯视三台正交相机；
5. 配置低反光材质、柔和灯光和 Freestyle 工程线稿；
6. 在 3D View 右侧 N 面板增加 ``Panthera`` 手调面板。

该脚本只处理本地模型文件，不连接 armd，也不会发送任何真机指令。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

import bmesh
import bpy
from bpy.props import StringProperty
from mathutils import Matrix, Quaternion, Vector


ROOT_COLLECTION = "Panthera_ThreeView"
RIG_ROOT_NAME = "PANTHERA_RIG_ROOT"
REFERENCE_COLLECTION = "REFERENCE_CAD"
DESIGN_COLLECTION = "EDITABLE_DESIGN"
CAMERA_COLLECTION = "ORTHO_CAMERAS"
LIGHT_COLLECTION = "SKETCH_LIGHTS"
EXACT_CAD_GLB = "panthera-ht-exact-cad.glb"
EXACT_CAD_BLEND = "panthera-ht-exact-cad.blend"
GRIPPER_LEFT_NAME = "GRIPPER_LEFT"
GRIPPER_RIGHT_NAME = "GRIPPER_RIGHT"
GRIPPER_TRAVEL_M = 0.042

JOINT_NAMES = tuple(f"J{i}_AXIS" for i in range(1, 7))
JOINT_ORIGINS = (
    (0.0, 0.0, 0.0584),
    (0.018199, 0.0, 0.053),
    (-0.26, 0.0, 0.0),
    (0.23, 0.0, 0.06),
    (0.07, 0.0, 0.036319),
    (0.02345, 0.0, -0.039),
)
JOINT_AXES = ("Z", "Y", "Y", "Y", "Z", "X")
JOINT_SIGNS = (1.0, 1.0, -1.0, -1.0, -1.0, 1.0)
JOINT_LIMITS = (
    (-2.4, 2.4),
    (-0.1, 3.2),
    (-0.1, 4.0),
    (-1.6, 1.6),
    (-1.7, 1.7),
    (-2.5, 2.5),
)

EDITABLE_PARTS = (
    ("EDIT_J2_J3_MainArm", "J2–J3 主臂"),
    ("EDIT_J3_SquareHousing", "J3 方形关节"),
    ("EDIT_J3_J4_UpperArm", "J3–J4 上臂"),
    ("EDIT_J5_SquareMotor", "J5 方形腕部"),
    ("EDIT_GripperBody", "夹爪主体"),
)


def find_repo_root() -> Path:
    candidates: list[Path] = []
    if "__file__" in globals():
        candidates.append(Path(__file__).resolve())
    if bpy.data.filepath:
        candidates.append(Path(bpy.data.filepath).resolve())
    candidates.append(Path.cwd().resolve())

    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / "proto" / "arm.proto").exists() and (parent / "vendor").exists():
                return parent
    raise RuntimeError("未找到 Panthera-WAM 仓库根目录；请从仓库内打开本脚本。")


def remove_collection_tree(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        remove_collection_tree(child)
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def fresh_collection(name: str, parent: bpy.types.Collection) -> bpy.types.Collection:
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def move_to_collection(obj: bpy.types.Object, collection: bpy.types.Collection) -> None:
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def material(
    name: str,
    color: tuple[float, float, float, float],
    roughness: float = 0.92,
    metallic: float = 0.0,
) -> bpy.types.Material:
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.diffuse_color = color
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = color
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.16
        elif "Specular" in bsdf.inputs:
            bsdf.inputs["Specular"].default_value = 0.20
    return mat


def new_mesh_object(
    name: str,
    mesh: bpy.types.Mesh,
    collection: bpy.types.Collection,
    parent: bpy.types.Object | None,
    location: Iterable[float] = (0.0, 0.0, 0.0),
    rotation: Quaternion | None = None,
    mat: bpy.types.Material | None = None,
) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    if rotation is not None:
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = rotation
    if mat is not None:
        obj.data.materials.append(mat)
    obj["panthera_editable"] = True
    return obj


def add_bevel(obj: bpy.types.Object, width: float, segments: int = 3) -> None:
    if width <= 0:
        return
    modifier = obj.modifiers.new("Soft industrial edges", "BEVEL")
    modifier.width = width
    modifier.segments = segments
    modifier.limit_method = "ANGLE"


def box(
    name: str,
    dimensions: tuple[float, float, float],
    collection: bpy.types.Collection,
    parent: bpy.types.Object | None,
    location: Iterable[float] = (0.0, 0.0, 0.0),
    rotation: Quaternion | None = None,
    bevel: float = 0.003,
    mat: bpy.types.Material | None = None,
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    scale = Matrix.Diagonal((*dimensions, 1.0))
    bmesh.ops.transform(bm, matrix=scale, verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    obj = new_mesh_object(name, mesh, collection, parent, location, rotation, mat)
    add_bevel(obj, min(bevel, min(dimensions) * 0.45), 4)
    obj["shape"] = "editable_box"
    obj["dimensions_m"] = dimensions
    return obj


def cylinder(
    name: str,
    radius: float,
    depth: float,
    axis: str,
    collection: bpy.types.Collection,
    parent: bpy.types.Object | None,
    location: Iterable[float] = (0.0, 0.0, 0.0),
    mat: bpy.types.Material | None = None,
    segments: int = 64,
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        cap_tris=False,
        segments=segments,
        radius1=radius,
        radius2=radius,
        depth=depth,
    )
    bm.to_mesh(mesh)
    bm.free()
    target = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}[axis]
    rotation = Vector((0, 0, 1)).rotation_difference(target)
    obj = new_mesh_object(name, mesh, collection, parent, location, rotation, mat)
    obj["shape"] = f"cylinder_axis_{axis}"
    return obj


def beam(
    name: str,
    start: Iterable[float],
    end: Iterable[float],
    thickness_y: float,
    thickness_z: float,
    collection: bpy.types.Collection,
    parent: bpy.types.Object,
    mat: bpy.types.Material,
    bevel: float = 0.005,
) -> bpy.types.Object:
    start_v = Vector(start)
    end_v = Vector(end)
    vector = end_v - start_v
    length = vector.length
    rotation = Vector((1, 0, 0)).rotation_difference(vector.normalized())
    obj = box(
        name,
        (length, thickness_y, thickness_z),
        collection,
        parent,
        (start_v + end_v) * 0.5,
        rotation,
        bevel,
        mat,
    )
    obj["beam_start_local"] = tuple(start_v)
    obj["beam_end_local"] = tuple(end_v)
    return obj


def add_slots(
    beam_obj: bpy.types.Object,
    collection: bpy.types.Collection,
    dark_mat: bpy.types.Material,
    tilt_deg: float,
) -> None:
    for index, fraction in enumerate((0.27, 0.41, 0.55, 0.69), start=1):
        for side_name, side_sign in (("A", -1.0), ("B", 1.0)):
            slot = box(
                f"{beam_obj.name}_Slot_{index}_{side_name}",
                (0.011, 0.0045, 0.027),
                collection,
                beam_obj,
                (
                    (fraction - 0.5) * beam_obj.dimensions.x,
                    side_sign * beam_obj.dimensions.y * 0.51,
                    0.0,
                ),
                Quaternion(Vector((0, 1, 0)), math.radians(tilt_deg)),
                0.0053,
                dark_mat,
            )
            slot["panthera_role"] = "decorative_slot"


def empty(
    name: str,
    collection: bpy.types.Collection,
    parent: bpy.types.Object | None,
    location: Iterable[float],
    display_size: float = 0.025,
) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    obj.empty_display_type = "ARROWS"
    obj.empty_display_size = display_size
    obj.parent = parent
    obj.location = location
    return obj


def add_joint_driver(joint: bpy.types.Object, axis: str, sign: float, limits: tuple[float, float]) -> None:
    joint.rotation_mode = "XYZ"
    joint["q_rad"] = 0.0
    joint["axis"] = axis
    joint["axis_sign"] = sign
    joint["soft_limit_lower"] = limits[0]
    joint["soft_limit_upper"] = limits[1]
    try:
        joint.id_properties_ui("q_rad").update(
            min=limits[0],
            max=limits[1],
            soft_min=limits[0],
            soft_max=limits[1],
            subtype="ANGLE",
            description="仅驱动 Blender 模型，不连接真机",
        )
    except TypeError:
        joint.id_properties_ui("q_rad").update(min=limits[0], max=limits[1])
    axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
    curve = joint.driver_add("rotation_euler", axis_index)
    driver = curve.driver
    driver.type = "SCRIPTED"
    variable = driver.variables.new()
    variable.name = "q"
    variable.type = "SINGLE_PROP"
    variable.targets[0].id = joint
    variable.targets[0].data_path = '["q_rad"]'
    driver.expression = f"{sign:g} * q"


def exact_cad_material() -> bpy.types.Material:
    """供精确 CAD 导出的低反光中性材质；不包含视口黑色线框。"""
    return material("MAT_ExactCAD", (0.56, 0.61, 0.65, 1.0), 0.82, 0.08)


def prepare_exact_cad_export(context: bpy.types.Context) -> list[bpy.types.Object]:
    """把官方 STL 作为实体导出主体，并返回需要写入 GLB 的对象。"""
    reference = bpy.data.collections.get(REFERENCE_COLLECTION)
    rig = bpy.data.collections.get("RIG")
    if reference is None or rig is None:
        raise RuntimeError("场景中缺少 REFERENCE_CAD 或 RIG；请先运行建场脚本。")

    design = bpy.data.collections.get(DESIGN_COLLECTION)
    if design is not None:
        design.hide_viewport = True
        design.hide_render = True

    reference.hide_viewport = False
    reference.hide_render = False
    cad_material = exact_cad_material()
    reference_objects = list(reference.all_objects)
    for obj in reference_objects:
        obj.hide_set(False)
        obj.hide_render = False
        obj.display_type = "SOLID"
        obj.show_in_front = False
        if obj.type == "MESH":
            obj.data.materials.clear()
            obj.data.materials.append(cad_material)
            obj["export_geometry"] = "official_panthera_ht_stl"

    rig_objects = list(rig.all_objects)
    for obj in rig_objects:
        obj.hide_set(False)
        obj.hide_render = False

    bpy.ops.object.select_all(action="DESELECT")
    export_objects = rig_objects + reference_objects
    for obj in export_objects:
        obj.select_set(True)
    rig_root = bpy.data.objects.get(RIG_ROOT_NAME)
    if rig_root is not None:
        context.view_layer.objects.active = rig_root

    context.scene["panthera_export_geometry"] = (
        "official follower STL: base_link + link1..link5 + split link6 body/left/right gripper"
    )
    context.scene["panthera_export_joint_order"] = ",".join(JOINT_NAMES)
    context.scene["panthera_export_gripper_nodes"] = f"{GRIPPER_LEFT_NAME},{GRIPPER_RIGHT_NAME}"
    context.view_layer.update()
    return export_objects


def export_exact_cad_files(
    context: bpy.types.Context,
    output_dir: Path | None = None,
    save_blend: bool = True,
) -> tuple[Path, Path | None]:
    """导出严格贴合 CAD 参考线的 GLB，并可另存一份干净 Blender 文件。"""
    repo_root = Path(context.scene.get("panthera_repo_root") or find_repo_root())
    export_dir = output_dir or repo_root / "docs" / "blender" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    glb_path = export_dir / EXACT_CAD_GLB
    blend_path = export_dir / EXACT_CAD_BLEND if save_blend else None

    prepare_exact_cad_export(context)
    bpy.ops.export_scene.gltf(
        filepath=str(glb_path),
        export_format="GLB",
        use_selection=True,
        export_materials="EXPORT",
        export_normals=True,
        export_cameras=False,
        export_lights=False,
        export_extras=True,
        export_animations=False,
        export_apply=False,
        export_yup=True,
    )
    if blend_path is not None:
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), copy=True, compress=True)

    print(f"[Panthera] 精确 CAD GLB 已导出：{glb_path}")
    if blend_path is not None:
        print(f"[Panthera] 精确 CAD Blender 文件已导出：{blend_path}")
    return glb_path, blend_path


def import_stl_reference(
    filepath: Path,
    name: str,
    collection: bpy.types.Collection,
    parent: bpy.types.Object,
) -> bpy.types.Object:
    if not filepath.exists():
        raise FileNotFoundError(
            f"缺少 {filepath}。请先执行：git submodule update --init vendor/Panthera-HT_SDK"
        )
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "stl_import"):
        bpy.ops.wm.stl_import(filepath=str(filepath))
    else:
        bpy.ops.import_mesh.stl(filepath=str(filepath))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    if not imported:
        raise RuntimeError(f"Blender 未从 {filepath.name} 返回导入对象")
    obj = imported[0]
    obj.name = f"REF_{name}"
    move_to_collection(obj, collection)
    obj.parent = parent
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj.display_type = "WIRE"
    obj.color = (0.18, 0.26, 0.31, 0.24)
    obj.hide_render = True
    obj.show_in_front = True
    obj["source_stl"] = str(filepath)
    obj["panthera_reference"] = True
    return obj


def mesh_from_face_indices(source: bpy.types.Mesh, face_indices: set[int], name: str) -> bpy.types.Mesh:
    """复制指定三角面，并在新网格中重建紧凑顶点索引。"""
    vertex_map: dict[int, int] = {}
    vertices: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    for face_index in sorted(face_indices):
        polygon = source.polygons[face_index]
        target_face: list[int] = []
        for source_vertex_index in polygon.vertices:
            target_index = vertex_map.get(source_vertex_index)
            if target_index is None:
                target_index = len(vertices)
                vertex_map[source_vertex_index] = target_index
                vertices.append(tuple(source.vertices[source_vertex_index].co))
            target_face.append(target_index)
        faces.append(target_face)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    return mesh


def classify_link6_faces(source: bpy.types.Mesh) -> dict[str, set[int]]:
    """按连通组件和局部包围盒把 link6 分成主体、+Y 左指和 -Y 右指。"""
    vertex_faces: list[list[int]] = [[] for _ in source.vertices]
    for polygon in source.polygons:
        for vertex_index in polygon.vertices:
            vertex_faces[vertex_index].append(polygon.index)

    remaining = set(range(len(source.polygons)))
    groups = {"body": set(), "left": set(), "right": set()}
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        component = {seed}
        component_vertices: set[int] = set()
        while stack:
            face_index = stack.pop()
            polygon = source.polygons[face_index]
            component_vertices.update(polygon.vertices)
            for vertex_index in polygon.vertices:
                for neighbor in vertex_faces[vertex_index]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        stack.append(neighbor)

        minimum = Vector((float("inf"),) * 3)
        maximum = Vector((float("-inf"),) * 3)
        for vertex_index in component_vertices:
            coordinate = source.vertices[vertex_index].co
            minimum.x = min(minimum.x, coordinate.x)
            minimum.y = min(minimum.y, coordinate.y)
            minimum.z = min(minimum.z, coordinate.z)
            maximum.x = max(maximum.x, coordinate.x)
            maximum.y = max(maximum.y, coordinate.y)
            maximum.z = max(maximum.z, coordinate.z)
        if maximum.x > 0.055 and minimum.y >= 0.040:
            groups["left"].update(component)
        elif maximum.x > 0.055 and maximum.y <= -0.040:
            groups["right"].update(component)
        else:
            groups["body"].update(component)
    return groups


def split_link6_gripper(
    link6: bpy.types.Object,
    reference_collection: bpy.types.Collection,
    rig_collection: bpy.types.Collection,
    j6: bpy.types.Object,
    rig_root: bpy.types.Object,
) -> tuple[bpy.types.Object, bpy.types.Object, bpy.types.Object]:
    """拆分官方 link6，并建立可由归一化开度驱动的左右夹指节点。"""
    source_mesh = link6.data
    groups = classify_link6_faces(source_mesh)
    if not groups["left"] or not groups["right"] or not groups["body"]:
        raise RuntimeError("link6 夹指自动拆分失败：未找到完整的主体/左右指面组")

    body_mesh = mesh_from_face_indices(source_mesh, groups["body"], "link6_body")
    left_mesh = mesh_from_face_indices(source_mesh, groups["left"], "gripper_left")
    right_mesh = mesh_from_face_indices(source_mesh, groups["right"], "gripper_right")

    link6.data = body_mesh
    link6.name = "REF_link6_body"
    link6["gripper_part"] = "body"

    left_node = empty(GRIPPER_LEFT_NAME, rig_collection, j6, (0.0, 0.0, 0.0), 0.012)
    right_node = empty(GRIPPER_RIGHT_NAME, rig_collection, j6, (0.0, 0.0, 0.0), 0.012)
    for node, side, close_direction in (
        (left_node, "left_positive_y", -1.0),
        (right_node, "right_negative_y", 1.0),
    ):
        node["gripper_side"] = side
        node["travel_axis"] = "Y"
        node["close_direction"] = close_direction
        node["travel_m"] = GRIPPER_TRAVEL_M
        node["opening_range"] = "0=closed,1=open"
        curve = node.driver_add("location", 1)
        variable = curve.driver.variables.new()
        variable.name = "g"
        variable.type = "SINGLE_PROP"
        variable.targets[0].id = rig_root
        variable.targets[0].data_path = '["gripper_opening"]'
        curve.driver.expression = f"{close_direction:g} * {GRIPPER_TRAVEL_M:g} * (1 - g)"

    left = bpy.data.objects.new("REF_gripper_left", left_mesh)
    right = bpy.data.objects.new("REF_gripper_right", right_mesh)
    for obj, node, side in (
        (left, left_node, "left_positive_y"),
        (right, right_node, "right_negative_y"),
    ):
        reference_collection.objects.link(obj)
        obj.parent = node
        obj.location = (0.0, 0.0, 0.0)
        obj.rotation_euler = (0.0, 0.0, 0.0)
        obj.scale = (1.0, 1.0, 1.0)
        obj.display_type = "WIRE"
        obj.color = link6.color
        obj.hide_render = True
        obj.show_in_front = True
        obj["source_stl"] = link6.get("source_stl", "link6.STL")
        obj["panthera_reference"] = True
        obj["gripper_part"] = side

    bpy.data.meshes.remove(source_mesh)
    print(
        "[Panthera] link6 已拆分："
        f"body={len(groups['body'])} faces, left={len(groups['left'])}, right={len(groups['right'])}"
    )
    return link6, left, right


def point_camera(camera: bpy.types.Object, target: Iterable[float]) -> None:
    direction = Vector(target) - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def camera(
    name: str,
    collection: bpy.types.Collection,
    location: tuple[float, float, float],
    target: tuple[float, float, float],
    ortho_scale: float,
    mirror_x: bool = False,
) -> bpy.types.Object:
    data = bpy.data.cameras.new(f"{name}_DATA")
    data.type = "ORTHO"
    data.ortho_scale = ortho_scale
    data.lens = 50
    data.passepartout_alpha = 0.82
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    obj.location = location
    point_camera(obj, target)
    if mirror_x:
        obj.scale.x = -1.0
        obj["mirrored_to_match_draft"] = True
    return obj


def area_light(
    name: str,
    collection: bpy.types.Collection,
    location: tuple[float, float, float],
    energy: float,
    size: float,
) -> bpy.types.Object:
    data = bpy.data.lights.new(f"{name}_DATA", "AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    obj.location = location
    point_camera(obj, (0.0, 0.0, 0.12))
    return obj


def configure_freestyle(scene: bpy.types.Scene) -> None:
    try:
        scene.render.use_freestyle = True
        settings = scene.view_layers[0].freestyle_settings
        lineset = settings.linesets[0]
        lineset.name = "Panthera clean technical lines"
        for property_name, value in {
            "select_silhouette": True,
            "select_border": True,
            "select_contour": True,
            "select_external_contour": True,
            "select_crease": True,
            "select_edge_mark": True,
            "select_material_boundary": False,
            "select_suggestive_contour": False,
            "select_ridge_valley": False,
        }.items():
            if hasattr(lineset, property_name):
                setattr(lineset, property_name, value)
        linestyle = lineset.linestyle
        linestyle.color = (0.055, 0.075, 0.085)
        linestyle.alpha = 0.86
        linestyle.thickness = 1.15
        if hasattr(linestyle, "caps"):
            linestyle.caps = "ROUND"
    except Exception as exc:  # Blender 小版本差异不应阻断建场。
        print(f"[Panthera] Freestyle 配置跳过：{exc}")


def configure_scene(scene: bpy.types.Scene, camera_side: bpy.types.Object, light_collection: bpy.types.Collection) -> None:
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        try:
            scene.render.engine = "BLENDER_EEVEE"
        except TypeError:
            pass
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 900
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.camera = camera_side

    world = scene.world or bpy.data.worlds.new("Panthera_Sketch_World")
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (0.94, 0.955, 0.965, 1.0)
        background.inputs["Strength"].default_value = 0.72

    area_light("LIGHT_Key_Softbox", light_collection, (-1.5, -2.4, 3.0), 780, 4.0)
    area_light("LIGHT_Fill_Softbox", light_collection, (2.3, 1.8, 2.0), 360, 3.2)
    configure_freestyle(scene)

    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except (TypeError, ValueError):
        pass


def configure_viewport() -> None:
    screen = bpy.context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        shading = area.spaces.active.shading
        shading.type = "MATERIAL"
        shading.light = "STUDIO"
        shading.show_shadows = False
        shading.show_cavity = True
        shading.cavity_type = "WORLD"
        shading.curvature_ridge_factor = 1.25
        shading.curvature_valley_factor = 0.65
        area.spaces.active.region_3d.view_perspective = "CAMERA"


def hide_default_startup_objects() -> None:
    """隐藏 Blender 新文件自带的 Cube/Camera/Light，避免盖住模型。"""
    for name in ("Cube", "Camera", "Light"):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.get("panthera_editable") or obj.get("panthera_reference"):
            continue
        obj.hide_set(True)
        obj.hide_render = True


def build_scene() -> None:
    repo_root = find_repo_root()
    scene = bpy.context.scene
    hide_default_startup_objects()
    old = bpy.data.collections.get(ROOT_COLLECTION)
    if old is not None:
        remove_collection_tree(old)

    root_collection = fresh_collection(ROOT_COLLECTION, scene.collection)
    rig_collection = fresh_collection("RIG", root_collection)
    reference_collection = fresh_collection(REFERENCE_COLLECTION, root_collection)
    design_collection = fresh_collection(DESIGN_COLLECTION, root_collection)
    camera_collection = fresh_collection(CAMERA_COLLECTION, root_collection)
    light_collection = fresh_collection(LIGHT_COLLECTION, root_collection)

    body = material("MAT_SketchBody", (0.73, 0.77, 0.79, 1.0), 0.96, 0.0)
    panel = material("MAT_SketchPanel", (0.55, 0.62, 0.66, 1.0), 0.93, 0.0)
    dark = material("MAT_SketchDark", (0.035, 0.055, 0.065, 1.0), 0.88, 0.0)

    rig_root = empty(RIG_ROOT_NAME, rig_collection, None, (0.0, 0.0, 0.0), 0.04)
    rig_root.empty_display_type = "PLAIN_AXES"
    rig_root["repo_root"] = str(repo_root)
    rig_root["safety"] = "visual-only; no armd or hardware I/O"
    rig_root["gripper_opening"] = 1.0
    try:
        rig_root.id_properties_ui("gripper_opening").update(
            min=0.0,
            max=1.0,
            soft_min=0.0,
            soft_max=1.0,
            description="夹爪归一化开度：0=闭合，1=完全张开；仅驱动 Blender 模型",
        )
    except TypeError:
        rig_root.id_properties_ui("gripper_opening").update(min=0.0, max=1.0)

    joints: list[bpy.types.Object] = []
    parent = rig_root
    for name, origin, axis, sign, limits in zip(
        JOINT_NAMES, JOINT_ORIGINS, JOINT_AXES, JOINT_SIGNS, JOINT_LIMITS, strict=True
    ):
        joint = empty(name, rig_collection, parent, origin)
        add_joint_driver(joint, axis, sign, limits)
        joints.append(joint)
        parent = joint

    mesh_dir = (
        repo_root
        / "vendor"
        / "Panthera-HT_SDK"
        / "panthera_python"
        / "Panthera-HT_description"
        / "meshes"
    )
    import_stl_reference(mesh_dir / "base_link.STL", "base_link", reference_collection, rig_root)
    imported_links: list[bpy.types.Object] = []
    for index, joint in enumerate(joints, start=1):
        imported_links.append(
            import_stl_reference(mesh_dir / f"link{index}.STL", f"link{index}", reference_collection, joint)
        )

    j1, j2, j3, j4, j5, j6 = joints
    split_link6_gripper(imported_links[5], reference_collection, rig_collection, j6, rig_root)

    # 底座与肩部：保持真实比例，但减少细碎结构。
    box("EDIT_BasePlate", (0.15, 0.12, 0.012), design_collection, rig_root, (0, 0, 0.006), bevel=0.003, mat=body)
    cylinder("EDIT_BaseMotor", 0.041, 0.058, "Z", design_collection, rig_root, (0, 0, 0.040), body)
    cylinder("EDIT_J1_Turntable", 0.047, 0.021, "Z", design_collection, j1, (0, 0, 0.004), panel)
    beam("EDIT_J1_ShoulderRiser", (0, 0, 0), JOINT_ORIGINS[1], 0.064, 0.054, design_collection, j1, body)

    # J2 为圆形；J2-J3 主臂是明确的直矩形。
    cylinder("EDIT_J2_RoundHousing", 0.044, 0.066, "Y", design_collection, j2, mat=body)
    cylinder("EDIT_J2_Hub", 0.019, 0.070, "Y", design_collection, j2, mat=panel)
    main_arm = beam(
        "EDIT_J2_J3_MainArm",
        (0, 0, 0),
        JOINT_ORIGINS[2],
        0.058,
        0.048,
        design_collection,
        j2,
        body,
        0.006,
    )
    add_slots(main_arm, design_collection, dark, -18.0)

    # J3 方形外壳 + 圆轴心；上臂保持平直，终点对齐 J4 高度。
    box("EDIT_J3_SquareHousing", (0.068, 0.066, 0.068), design_collection, j3, bevel=0.006, mat=body)
    cylinder("EDIT_J3_Hub", 0.019, 0.071, "Y", design_collection, j3, mat=panel)
    upper_arm = beam(
        "EDIT_J3_J4_UpperArm",
        (0, 0, 0.060),
        (0.23, 0, 0.060),
        0.056,
        0.046,
        design_collection,
        j3,
        body,
        0.006,
    )
    add_slots(upper_arm, design_collection, dark, -16.0)

    # 腕部：逐级收小，用方圆交替表达不同旋转轴。
    cylinder("EDIT_J4_RoundHousing", 0.036, 0.060, "Y", design_collection, j4, mat=body)
    beam("EDIT_J4_J5_WristLink", (0, 0, 0), JOINT_ORIGINS[4], 0.052, 0.043, design_collection, j4, body)
    box("EDIT_J5_SquareMotor", (0.058, 0.062, 0.060), design_collection, j5, bevel=0.005, mat=body)
    cylinder("EDIT_J5_TopMotor", 0.026, 0.040, "Z", design_collection, j5, (0, 0, 0.046), panel)
    beam("EDIT_J5_J6_Offset", (0, 0, 0), JOINT_ORIGINS[5], 0.045, 0.039, design_collection, j5, body)

    # 相机只保留两块辨识轮廓，避免重新堆出复杂 CAD 线。
    box("EDIT_CameraStem", (0.012, 0.018, 0.052), design_collection, j5, (0.012, 0, 0.071), bevel=0.0025, mat=dark)
    box(
        "EDIT_CameraHead",
        (0.036, 0.030, 0.042),
        design_collection,
        j5,
        (0.024, 0, 0.108),
        Quaternion(Vector((0, 1, 0)), math.radians(-17.0)),
        0.004,
        dark,
    )

    # J6 与夹爪：保留圆形滚转轴、方形主体与两指结构。
    cylinder("EDIT_J6_Roll", 0.029, 0.046, "X", design_collection, j6, mat=panel)
    box("EDIT_GripperBody", (0.084, 0.055, 0.044), design_collection, j6, (0.047, 0, 0), bevel=0.005, mat=dark)
    box("EDIT_GripperCarriage", (0.028, 0.074, 0.048), design_collection, j6, (0.101, 0, 0), bevel=0.004, mat=panel)
    for side, y in (("L", -0.027), ("R", 0.027)):
        box(f"EDIT_GripperFinger_{side}", (0.067, 0.012, 0.016), design_collection, j6, (0.146, y, 0), bevel=0.005, mat=body)
        box(f"EDIT_GripperTip_{side}", (0.025, 0.015, 0.020), design_collection, j6, (0.181, y, 0), bevel=0.008, mat=body)

    side = camera("CAM_SIDE_DRAFT_MATCH", camera_collection, (0, 3.0, 0.13), (-0.01, 0, 0.13), 0.34, True)
    front = camera("CAM_FRONT", camera_collection, (3.0, 0, 0.13), (0, 0, 0.13), 0.34)
    top = camera("CAM_TOP", camera_collection, (0, 0, 3.0), (-0.01, 0, 0), 0.24)
    side["view_label"] = "SIDE / X-Z / draft matched"
    front["view_label"] = "FRONT / Y-Z"
    top["view_label"] = "TOP / X-Y"

    configure_scene(scene, side, light_collection)
    configure_viewport()
    scene["panthera_repo_root"] = str(repo_root)
    scene["panthera_render_dir"] = str(repo_root / "docs" / "blender" / "renders")

    bpy.context.view_layer.objects.active = main_arm
    main_arm.select_set(True)
    print("[Panthera] 三视图手调场景已生成。打开 3D View > N > Panthera 开始调整。")


class PANTHERA_OT_set_camera(bpy.types.Operator):
    bl_idname = "panthera.set_camera"
    bl_label = "切换 Panthera 正交相机"
    camera_name: StringProperty()

    def execute(self, context: bpy.types.Context):
        camera_obj = bpy.data.objects.get(self.camera_name)
        if camera_obj is None:
            self.report({"ERROR"}, f"未找到 {self.camera_name}")
            return {"CANCELLED"}
        context.scene.camera = camera_obj
        if context.screen is not None:
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.region_3d.view_perspective = "CAMERA"
        return {"FINISHED"}


class PANTHERA_OT_select_part(bpy.types.Operator):
    bl_idname = "panthera.select_part"
    bl_label = "选择可编辑部件"
    object_name: StringProperty()

    def execute(self, context: bpy.types.Context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            self.report({"ERROR"}, f"未找到 {self.object_name}")
            return {"CANCELLED"}
        bpy.ops.object.select_all(action="DESELECT")
        obj.hide_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class PANTHERA_OT_reset_pose(bpy.types.Operator):
    bl_idname = "panthera.reset_pose"
    bl_label = "六轴归零（仅模型）"

    def execute(self, context: bpy.types.Context):
        for name in JOINT_NAMES:
            joint = bpy.data.objects.get(name)
            if joint is not None:
                joint["q_rad"] = 0.0
        rig_root = bpy.data.objects.get(RIG_ROOT_NAME)
        if rig_root is not None:
            rig_root["gripper_opening"] = 1.0
        context.view_layer.update()
        return {"FINISHED"}


class PANTHERA_OT_toggle_reference(bpy.types.Operator):
    bl_idname = "panthera.toggle_reference"
    bl_label = "显示/隐藏官方 CAD 线框"

    def execute(self, context: bpy.types.Context):
        collection = bpy.data.collections.get(REFERENCE_COLLECTION)
        if collection is None:
            return {"CANCELLED"}
        collection.hide_viewport = not collection.hide_viewport
        return {"FINISHED"}


class PANTHERA_OT_render_three(bpy.types.Operator):
    bl_idname = "panthera.render_three"
    bl_label = "渲染三视图 PNG"

    def execute(self, context: bpy.types.Context):
        output_dir = Path(context.scene.get("panthera_render_dir", "//renders"))
        output_dir.mkdir(parents=True, exist_ok=True)
        previous = context.scene.camera
        for camera_name, filename in (
            ("CAM_SIDE_DRAFT_MATCH", "side.png"),
            ("CAM_FRONT", "front.png"),
            ("CAM_TOP", "top.png"),
        ):
            camera_obj = bpy.data.objects.get(camera_name)
            if camera_obj is None:
                continue
            context.scene.camera = camera_obj
            context.scene.render.filepath = str(output_dir / filename)
            bpy.ops.render.render(write_still=True)
        context.scene.camera = previous
        self.report({"INFO"}, f"已输出到 {output_dir}")
        return {"FINISHED"}


class PANTHERA_OT_export_exact_cad(bpy.types.Operator):
    bl_idname = "panthera.export_exact_cad"
    bl_label = "导出严格贴线 CAD（GLB + BLEND）"
    bl_description = "以官方黑线 STL 为实体导出主体，保留 J1-J6 层级并移除线框显示"

    def execute(self, context: bpy.types.Context):
        try:
            glb_path, blend_path = export_exact_cad_files(context)
        except Exception as exc:
            self.report({"ERROR"}, f"导出失败：{exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"已导出 {glb_path.name} 和 {blend_path.name if blend_path else ''}")
        return {"FINISHED"}


class PANTHERA_PT_three_view(bpy.types.Panel):
    bl_label = "Panthera 三视图手调"
    bl_idname = "PANTHERA_PT_three_view"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Panthera"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="仅编辑模型，不连接真机", icon="INFO")

        row = layout.row(align=True)
        for label, camera_name in (
            ("侧视", "CAM_SIDE_DRAFT_MATCH"),
            ("主视", "CAM_FRONT"),
            ("俯视", "CAM_TOP"),
        ):
            operator = row.operator("panthera.set_camera", text=label)
            operator.camera_name = camera_name

        box_layout = layout.box()
        box_layout.label(text="六轴模型状态（rad）")
        for index, name in enumerate(JOINT_NAMES, start=1):
            joint = bpy.data.objects.get(name)
            if joint is not None:
                box_layout.prop(joint, '["q_rad"]', text=f"J{index}", slider=True)
        rig_root = bpy.data.objects.get(RIG_ROOT_NAME)
        if rig_root is not None:
            box_layout.prop(rig_root, '["gripper_opening"]', text="夹爪开度", slider=True)
        box_layout.operator("panthera.reset_pose", icon="LOOP_BACK")

        parts = layout.box()
        parts.label(text="常用可编辑部件")
        for object_name, label in EDITABLE_PARTS:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                continue
            operator = parts.operator("panthera.select_part", text=label, icon="EDITMODE_HLT")
            operator.object_name = object_name
            parts.prop(obj, "scale", text="XYZ 比例")

        layout.operator("panthera.toggle_reference", icon="XRAY")
        layout.operator("panthera.render_three", icon="RENDER_STILL")
        layout.operator("panthera.export_exact_cad", icon="EXPORT")


REGISTER_CLASSES = (
    PANTHERA_OT_set_camera,
    PANTHERA_OT_select_part,
    PANTHERA_OT_reset_pose,
    PANTHERA_OT_toggle_reference,
    PANTHERA_OT_render_three,
    PANTHERA_OT_export_exact_cad,
    PANTHERA_PT_three_view,
)


def register_ui() -> None:
    for cls in reversed(REGISTER_CLASSES):
        existing = getattr(bpy.types, cls.__name__, None)
        if existing is not None:
            try:
                bpy.utils.unregister_class(existing)
            except RuntimeError:
                pass
    for cls in REGISTER_CLASSES:
        bpy.utils.register_class(cls)


if __name__ == "__main__":
    register_ui()
    build_scene()
