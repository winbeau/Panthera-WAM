# Panthera Windows 简单视频查看器

## 安装

双击 `install.bat`，或者在 PowerShell 中执行：

```powershell
py -m pip install -r requirements.txt
```

## 直接看视频流

```powershell
py view_camera.py --source overhead
py view_camera.py --source wrist
```

也可以直接双击：

- `run_overhead.bat`：俯视相机
- `run_wrist.bat`：腕部相机

窗口中按 `q` 或 `Esc` 退出。默认连接 Pi 的 `100.78.118.74`：

- wrist: `100.78.118.74:50052`
- overhead: `100.78.118.74:50053`

## 如果 Windows 不能直连 Tailscale 地址

先在 PowerShell 单独开一个窗口建立 SSH 隧道：

```powershell
ssh -N -L 50052:127.0.0.1:50052 -L 50053:127.0.0.1:50053 winbeau@100.78.118.74
```

然后在另一个窗口运行：

```powershell
py view_camera.py --source overhead --host 127.0.0.1
```

脚本使用 Python + OpenCV + gRPC，不使用 WPF。
