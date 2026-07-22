# Panthera-HT 六轴实时三视图

最终展示页：`panthera-six-axis-three-view.html`。

页面使用 `docs/blender/exports/panthera-ht-exact-cad.glb` 中的官方 CAD 网格和
`J1_AXIS`–`J6_AXIS` 层级。每次状态更新只修改六个关节节点一次，侧视、主视、俯视
由三台正交相机对同一个场景分别渲染，不维护三份二维模型。

## 启动精确 CAD 版本

GLB 需要通过 HTTP 加载。在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File docs/mockups/serve-panthera-three-view.ps1
```

脚本会打开：

```text
http://127.0.0.1:8765/docs/mockups/panthera-six-axis-three-view.html
```

直接双击 HTML 也能显示，但由于浏览器禁止 `file://` 页面读取外部 GLB，会自动使用
仓库中的轻量 CAD 备用网格，并在底部明确标记 `LIGHTWEIGHT`。

## 六轴状态接入

直接调用：

```javascript
PantheraThreeView.setJoints([q1, q2, q3, q4, q5, q6], {
  source: "armd",
  timestamp: performance.now()
});
```

传入状态对象：

```javascript
PantheraThreeView.setState({
  positions: [q1, q2, q3, q4, q5, q6],
  source: "armd",
  timestamp: performance.now()
});
```

也可以使用页面事件，不要求调用方持有组件引用：

```javascript
window.dispatchEvent(new CustomEvent("panthera:joints", {
  detail: {
    positions: [q1, q2, q3, q4, q5, q6],
    source: "armd",
    timestamp: performance.now()
  }
}));
```

接口同时接受 `{ joints: [{ position }, ...] }`，便于直接适配状态流响应。所有输入都会在
浏览器侧按六轴软限位夹紧。本页面只做可视化，不连接 `armd`，也不会发送运动指令。

## 显示风格

页面右上角可以实时切换七种 Blender 常用显示思路：工程线稿、实体模式、材质预览、
渲染质感、蓝图模式、X-Ray 和线框模式。所有风格共用同一套 GLB 和关节状态，不会重复
下载模型。

也可以通过 API 切换：

```javascript
PantheraThreeView.setStyle("blueprint");
PantheraThreeView.getStyles();
```

URL 参数同样可用，例如 `?style=wireframe`。

七张侧视预览与清单位于 `docs/blender/renders/styles/`。这些预览使用同一份 Blender
精确 CAD 导出模型生成，仅更换前端材质/着色方式，因此风格切换不会改变六轴层级、
关节状态或 TCP 计算结果。

## 镜头模式

- `follow` / 自动特写：每次姿态变化后根据整机包围盒自动平移、缩放，让机械臂尽量充满三个视口。
- `fixed-base` / 基座固定：三台正交相机的位置、观察中心和缩放固定；基座在画面中的位置始终不变，机械臂在固定工作空间内运动。

```javascript
PantheraThreeView.setCameraMode("fixed-base");
PantheraThreeView.getCameraModes();
```

URL 参数可写成 `?camera=fixed-base`。

## 夹爪开度

精确 GLB 和轻量备用网格都包含独立的 `GRIPPER_LEFT`、`GRIPPER_RIGHT` 节点。
归一化开度约定为 `0=闭合`、`1=完全张开`，两指沿局部 Y 方向对称移动，单侧最大行程
为 42 mm；完全闭合时两侧夹持面接触，不保留可见间隙。

```javascript
PantheraThreeView.setGripper(0.35);

PantheraThreeView.setState({
  positions: [q1, q2, q3, q4, q5, q6],
  gripper: 0.35,
  source: "armd"
});
```

也可以发送 `panthera:gripper` 事件，或使用 URL 参数 `?gripper=0.5`。
