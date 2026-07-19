# Panthera Terminal 品牌资产

这套标记从原参考图的“齿轮 + 三辐关节 + 末端夹爪”重新绘制，针对 Windows
任务栏、标题栏和安装器的小尺寸显示做了简化。六个齿对应 Panthera-HT 六轴，
三辐中心同时表达机器人关节与 World / Action / Model 三元关系。

## 文件

- `panthera-terminal-app-icon.svg`：带深色圆角底板的 Windows 应用图标母版。
- `panthera-terminal-app-icon-small.svg`：16/20/24 px 专用简化母版。
- `panthera-terminal-mark.svg`：透明底全彩品牌标记，适合浅色背景。
- `panthera-terminal-mark-mono.svg`：单色标记；嵌入网页时可通过 CSS `color` 改色。
- `panthera-terminal-mark-white.svg`：深色背景直接使用的反白标记。
- `Panthera.Terminal.ico`：包含 16/20/24/32/40/48/64/128/256 px 的 Windows 图标。
- `panthera-terminal-titlebar.png`：64 px 标题栏资源。
- `png/`：从两份应用图标母版生成的 16–512 px PNG 尺寸集，以及全彩/反白标记的 512 px PNG。

## 色板

- Windows 蓝：`#0067C0`
- 高亮蓝：`#37A5FF`
- 深海军蓝：`#07131F`
- 石墨黑：`#111820`

保留标记外缘至少 8% 的安全区。小于 32 px 时使用带底板的应用图标，不使用透明底
全彩标记。所有文件均为本项目原创矢量，不依赖外部字体或图标库。

重新生成 PNG/ICO：

```powershell
pwsh ./wpf/tools/generate-brand-assets.ps1
```
