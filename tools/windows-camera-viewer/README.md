# Panthera Windows 简单视频查看器

这是一个纯 Python + OpenCV 的实时视频窗口，不使用 WPF，也不会把帧先保存成图片。

## 安装

双击 `install.bat`，或者在 PowerShell 中执行：

```powershell
py -m pip install -r requirements.txt
```

## 直接看实时视频流

默认使用 Windows OpenSSH 配置里的 `pi5` alias，脚本会自动建立 SSH 隧道：

```powershell
py view_camera.py --source overhead
py view_camera.py --source wrist
```

也可以直接双击：

- `run_overhead.bat`：俯视相机
- `run_wrist.bat`：腕部相机

窗口中按 `q` 或 `Esc` 退出。

默认读取：

```text
配置文件：%USERPROFILE%\.ssh\config
SSH alias：pi5
远端 HostName：192.168.10.249
wrist camera：远端 50052
 overhead camera：远端 50053
```

脚本通过以下方式转发相机端口：

```text
ssh pi5 -L 127.0.0.1:15053:127.0.0.1:50053
```

## 直连模式

如果 Windows 可以直接访问 Pi 的局域网地址，不需要 SSH 隧道：

```powershell
py view_camera.py --source overhead --host 192.168.10.249
```

如果 alias 不叫 `pi5`：

```powershell
py view_camera.py --source overhead --ssh-alias your_alias
```

如果 SSH 配置不在默认位置：

```powershell
py view_camera.py --source overhead --ssh-config C:\Users\genev\.ssh\config
```

查看腕部深度流：

```powershell
py view_camera.py --source wrist --stream depth
```
