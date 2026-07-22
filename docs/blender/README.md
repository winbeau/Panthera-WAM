# Panthera-HT Blender 三视图手调模板

`panthera_three_view_setup.py` 会从仓库内的官方 follower STL/URDF 基线生成一个可手调的 Blender 场景。它只处理模型，不连接 `armd`，也不会访问机械臂硬件。

## 运行

1. 确保 SDK 子模块已初始化：

   ```powershell
   git submodule update --init vendor/Panthera-HT_SDK
   ```

2. 打开 Blender，进入 **Scripting** 工作区。
3. 点击 **Open**，选择：

   ```text
   docs/blender/panthera_three_view_setup.py
   ```

4. 点击 **Run Script**。
5. 回到 3D View，按 `N` 打开右侧栏，选择 **Panthera** 页签。

也可以从仓库根目录用命令行启动：

```powershell
blender --python docs/blender/panthera_three_view_setup.py
```

## 场景结构

```text
Panthera_ThreeView
├─ RIG
│  └─ PANTHERA_RIG_ROOT
│     └─ J1_AXIS → J2_AXIS → … → J6_AXIS
├─ REFERENCE_CAD       官方 STL，仅作线框参照，不参与渲染
├─ EDITABLE_DESIGN     可直接修改的简化设计外壳
├─ ORTHO_CAMERAS       侧视 / 主视 / 俯视正交相机
└─ SKETCH_LIGHTS       柔和面光源
```

六个关节 Empty 都带 `q_rad` 自定义属性和软限位。右侧 Panthera 面板提供滑块；它们只驱动 Blender 层级。

官方 `link6.STL` 会在建场时自动按连通组件拆成 `REF_link6_body`、
`REF_gripper_left` 与 `REF_gripper_right`。左右夹指分别挂在 `GRIPPER_LEFT`、
`GRIPPER_RIGHT` 节点下，面板中的“夹爪开度”控制两指对称运动：`0` 为闭合，`1` 为张开。

## 建议优先调整的对象

| 对象 | 用途 | 推荐修改方式 |
|---|---|---|
| `EDIT_J2_J3_MainArm` | 下方主臂矩形轮廓 | `S` 缩放，或 `Tab` 进入 Edit Mode 移动端面 |
| `EDIT_J3_SquareHousing` | J3 方形关节盒 | 修改 XYZ 比例；保留内部圆形轴心 |
| `EDIT_J3_J4_UpperArm` | 上方回程主臂 | 调整厚度、槽孔位置和与 J4 的连接轮廓 |
| `EDIT_J5_SquareMotor` | 腕部方形电机 | 调整方圆比例，避免腕部堆叠过高 |
| `EDIT_GripperBody` | 夹爪主体 | 调整主体长度与双指间距 |

槽孔对象名称以 `_Slot_1`–`_Slot_4` 结尾，可以单独移动、旋转、缩放或删除。需要对照真实外轮廓时，点击面板中的“显示/隐藏官方 CAD 线框”。

## 三视图与线稿

- `CAM_SIDE_DRAFT_MATCH`：侧视图，水平镜像以贴近最初草图方向。
- `CAM_FRONT`：主视图。
- `CAM_TOP`：俯视图。
- 渲染使用低金属度、高粗糙度材质和 Freestyle 外轮廓/折线；不会显示 STL 三角网格线。
- 点击“渲染三视图 PNG”后，图片输出到 `docs/blender/renders/`。

定稿后可从 Blender 导出 GLB，把 `EDITABLE_DESIGN` 作为前端模型；J1–J6 的对象层级和名称已经固定，前端只需把六个弧度值写入对应局部旋转轴。

## 严格贴合官方 CAD 黑线导出

如果最终模型必须严格贴合黑色参考线，不要导出 `EDITABLE_DESIGN`。点击 Panthera 面板中的
“导出严格贴线 CAD（GLB + BLEND）”，脚本会直接使用官方 `base_link.STL` 与
`link1.STL`–`link6.STL` 作为实体几何，并输出：

- `docs/blender/exports/panthera-ht-exact-cad.glb`：前端可直接加载，保留 `J1_AXIS`–`J6_AXIS` 层级与关节元数据；
- `docs/blender/exports/panthera-ht-exact-cad.blend`：已隐藏简化白模、将官方 CAD 改为正常低反光实体材质的检查文件。

黑色线框只是 Blender 的参照显示方式，不会写进 GLB；导出的三角网格本身就是黑线所描出的官方 CAD 表面。
