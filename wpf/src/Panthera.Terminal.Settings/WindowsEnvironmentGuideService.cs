using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Settings;

public sealed class WindowsEnvironmentGuideService : IEnvironmentGuideService
{
    private const string ArmVidPid = "VID_CAF1&PID_FFFF";
    private const string D405VidPid = "VID_8086&PID_0B5B";
    private readonly IArmdClient _client;

    public WindowsEnvironmentGuideService(IArmdClient client)
    {
        _client = client;
    }

    public async Task<EnvironmentGuideResult> ProbeAsync(
        TerminalSettings settings,
        CancellationToken cancellationToken)
    {
        var steps = new List<EnvironmentGuideStep>();
        var usbipd = await RunAsync("usbipd", ["--version"], cancellationToken);
        steps.Add(Step("usbipd", usbipd.Success, usbipd.Success ? usbipd.Output.Trim() : usbipd.Error, usbipd.Command));

        var distributions = await RunAsync(
            "wsl.exe",
            ["-l", "--running", "-q"],
            cancellationToken,
            Encoding.Unicode);
        var wslRunning = distributions.Success && distributions.Output.Replace("\0", string.Empty)
            .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Contains(settings.WslDistribution, StringComparer.OrdinalIgnoreCase);
        steps.Add(Step("WSL 发行版", wslRunning,
            wslRunning ? $"{settings.WslDistribution} 正在运行" : $"{settings.WslDistribution} 未运行",
            distributions.Command));

        var device = await LocateDeviceAsync(ArmVidPid, settings.UsbSerial, cancellationToken);
        var camera = await LocateDeviceAsync(D405VidPid, string.Empty, cancellationToken);
        steps.Add(Step("机械臂 USB", device is not null,
            device is null ? $"未找到 {DescribeTarget(settings.UsbSerial)}" : $"已匹配 busid {device.BusId}",
            "usbipd state --json"));
        steps.Add(Step("D405 USB", camera is not null,
            camera is null ? $"未找到 {D405VidPid}" : $"已匹配 busid {camera.BusId}",
            "usbipd state --json"));

        var usbList = await RunAsync("usbipd", ["list"], cancellationToken);
        var stateLine = device is null ? string.Empty : FindBusLine(usbList.Output, device.BusId);
        var shared = stateLine.Contains("Shared", StringComparison.OrdinalIgnoreCase)
            || stateLine.Contains("Attached", StringComparison.OrdinalIgnoreCase);
        var attached = stateLine.Contains("Attached", StringComparison.OrdinalIgnoreCase);
        steps.Add(Step("机械臂 USB 共享", shared, shared ? stateLine.Trim() : "设备尚未 bind", "usbipd list"));
        steps.Add(Step("机械臂 USB 挂载", attached, attached ? stateLine.Trim() : "设备尚未 attach 到 WSL", "usbipd list"));

        var cameraLine = camera is null ? string.Empty : FindBusLine(usbList.Output, camera.BusId);
        var cameraAttached = cameraLine.Contains("Attached", StringComparison.OrdinalIgnoreCase);
        steps.Add(Step("D405 USB 挂载", cameraAttached,
            cameraAttached ? cameraLine.Trim() : "D405 尚未 attach 到 WSL",
            camera is null ? "usbipd list" : $"usbipd attach --wsl --busid {camera.BusId}"));

        var tty = await RunWslAsync(settings, "compgen -G '/dev/ttyACM*' | wc -l", cancellationToken);
        var ttyCount = 0;
        var ttyReady = tty.Success && int.TryParse(tty.Output.Trim(), out ttyCount) && ttyCount >= 4;
        steps.Add(Step("串口与权限", ttyReady,
            ttyReady ? $"检测到 {ttyCount} 个 ttyACM 设备" : $"需要至少 4 个 ttyACM 设备；当前输出：{tty.Output.Trim()}",
            tty.Command));

        try
        {
            var daemon = await _client.GetDaemonStatusAsync(cancellationToken);
            steps.Add(Step("armd 探活", true,
                daemon.HardwareConnected || daemon.Simulation ? "armd 可连接" : "armd 已启动但硬件未连接",
                $"gRPC {settings.Endpoint}"));
        }
        catch (Exception exception)
        {
            steps.Add(Step("armd 探活", false, exception.Message, $"gRPC {settings.Endpoint}"));
        }
        try
        {
            var status = await _client.GetCameraStatusAsync(cancellationToken);
            var healthy = status.Enabled && status.Available && status.Streaming;
            var detail = healthy
                ? $"{status.Model} · {status.ActualFps:F1} fps · {status.LastFrameAgeMs} ms"
                : status.Error;
            steps.Add(Step("D405 / camerad", healthy, detail, $"CameraService {settings.CameraEndpoint}"));
        }
        catch (Exception exception)
        {
            steps.Add(Step("D405 / camerad", false, exception.Message, $"CameraService {settings.CameraEndpoint}"));
        }
        return new EnvironmentGuideResult(steps);
    }

    public async Task<EnvironmentGuideResult> RunAsync(
        TerminalSettings settings,
        CancellationToken cancellationToken)
    {
        var steps = new List<EnvironmentGuideStep>();
        var usbipd = await RunAsync("usbipd", ["--version"], cancellationToken);
        steps.Add(Step("usbipd", usbipd.Success,
            usbipd.Success ? usbipd.Output.Trim() : "未安装；请执行 winget install --id dorssel.usbipd-win",
            usbipd.Command));
        if (!usbipd.Success)
        {
            return new EnvironmentGuideResult(steps);
        }

        var startWsl = await RunAsync(
            "wsl.exe",
            BuildWslArguments(settings, "true"),
            cancellationToken);
        steps.Add(Step("启动 WSL", startWsl.Success,
            startWsl.Success ? $"{settings.WslDistribution} 已启动" : startWsl.Error,
            startWsl.Command));
        if (!startWsl.Success)
        {
            return new EnvironmentGuideResult(steps);
        }

        var device = await LocateDeviceAsync(ArmVidPid, settings.UsbSerial, cancellationToken);
        var camera = await LocateDeviceAsync(D405VidPid, string.Empty, cancellationToken);
        steps.Add(Step("机械臂 USB", device is not null,
            device is null ? $"未找到 {DescribeTarget(settings.UsbSerial)}；请检查上电与 USB" : $"已匹配 busid {device.BusId}",
            "usbipd state --json"));
        steps.Add(Step("D405 USB", camera is not null,
            camera is null ? $"未找到 {D405VidPid}；请检查相机与 USB 3 连接" : $"已匹配 busid {camera.BusId}",
            "usbipd state --json"));
        if (device is null || camera is null)
        {
            return new EnvironmentGuideResult(steps);
        }

        if (!await EnsureUsbAttachedAsync("机械臂 USB", device, steps, cancellationToken)
            || !await EnsureUsbAttachedAsync("D405 USB", camera, steps, cancellationToken))
        {
            return new EnvironmentGuideResult(steps);
        }

        var cameraUsb = await RunWslAsync(settings, "lsusb -d 8086:0b5b", cancellationToken);
        steps.Add(Step("D405 WSL 可见", cameraUsb.Success,
            cameraUsb.Success ? cameraUsb.Output.Trim() : cameraUsb.Error,
            cameraUsb.Command));
        if (!cameraUsb.Success)
        {
            return new EnvironmentGuideResult(steps);
        }

        var tty = await RunWslAsync(settings, "compgen -G '/dev/ttyACM*' | wc -l", cancellationToken);
        var ttyCount = 0;
        var ttyReady = tty.Success && int.TryParse(tty.Output.Trim(), out ttyCount) && ttyCount >= 4;
        steps.Add(Step("串口与权限", ttyReady,
            ttyReady
                ? $"检测到 {ttyCount} 个 ttyACM 设备"
                : "串口未就绪；请安装 udev 规则 KERNEL==\"ttyACM*\", MODE=\"0777\"",
            tty.Command));
        if (!ttyReady)
        {
            return new EnvironmentGuideResult(steps);
        }

        var startServices = await RunWslAsync(
            settings,
            "systemctl --user start armd camerad",
            cancellationToken);
        steps.Add(Step("启动 Linux 后端", startServices.Success,
            startServices.Success ? "armd 与 camerad systemd user service 已启动" : startServices.Error,
            startServices.Command));
        if (!startServices.Success)
        {
            return new EnvironmentGuideResult(steps);
        }

        Exception? lastError = null;
        for (var attempt = 0; attempt < 20; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var daemon = await _client.GetDaemonStatusAsync(cancellationToken);
                var healthy = daemon.Simulation || daemon.HardwareConnected;
                steps.Add(Step("armd 探活", healthy,
                    healthy ? $"已联通，控制频率 {daemon.ControlHz:F0} Hz" : "armd 可连接但硬件尚未就绪",
                    $"gRPC {settings.Endpoint}"));
                if (!healthy)
                {
                    return new EnvironmentGuideResult(steps);
                }
                var cameraStatus = await _client.GetCameraStatusAsync(cancellationToken);
                var cameraHealthy = cameraStatus.Enabled && cameraStatus.Available && cameraStatus.Streaming;
                var cameraDetail = cameraHealthy
                    ? $"{cameraStatus.Model} · {cameraStatus.ActualFps:F1} fps"
                    : cameraStatus.Error;
                steps.Add(Step("D405 / camerad", cameraHealthy, cameraDetail,
                    $"CameraService {settings.CameraEndpoint}"));
                return new EnvironmentGuideResult(steps);
            }
            catch (Exception exception)
            {
                lastError = exception;
                await Task.Delay(500, cancellationToken);
            }
        }
        steps.Add(Step("armd 探活", false, lastError?.Message ?? "探活超时", $"gRPC {settings.Endpoint}"));
        return new EnvironmentGuideResult(steps);
    }

    private static EnvironmentGuideStep Step(string name, bool success, string detail, string command) =>
        new(name, success, detail, command);

    private static async Task<UsbDevice?> LocateDeviceAsync(
        string vidPid,
        string targetSerial,
        CancellationToken cancellationToken)
    {
        var state = await RunAsync("usbipd", ["state", "--json"], cancellationToken);
        if (!state.Success || string.IsNullOrWhiteSpace(state.Output))
        {
            return null;
        }
        using var document = JsonDocument.Parse(state.Output);
        return LocateDevice(document.RootElement, vidPid, targetSerial);
    }

    internal static UsbDevice? LocateDevice(JsonElement element, string targetSerial = "")
        => LocateDevice(element, ArmVidPid, targetSerial);

    internal static UsbDevice? LocateDevice(JsonElement element, string vidPid, string targetSerial)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            string? instanceId = null;
            string? busId = null;
            foreach (var property in element.EnumerateObject())
            {
                if (property.Name.Equals("InstanceId", StringComparison.OrdinalIgnoreCase))
                {
                    instanceId = property.Value.GetString();
                }
                else if (property.Name.Equals("BusId", StringComparison.OrdinalIgnoreCase))
                {
                    busId = property.Value.GetString();
                }
            }
            if (!string.IsNullOrWhiteSpace(instanceId)
                && !string.IsNullOrWhiteSpace(busId)
                && instanceId.Contains(vidPid, StringComparison.OrdinalIgnoreCase)
                && (string.IsNullOrWhiteSpace(targetSerial)
                    || instanceId.Contains(targetSerial, StringComparison.OrdinalIgnoreCase)))
            {
                return new UsbDevice(busId, instanceId);
            }
            foreach (var property in element.EnumerateObject())
            {
                var match = LocateDevice(property.Value, vidPid, targetSerial);
                if (match is not null)
                {
                    return match;
                }
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                var match = LocateDevice(item, vidPid, targetSerial);
                if (match is not null)
                {
                    return match;
                }
            }
        }
        return null;
    }

    private static string FindBusLine(string output, string busId) => output
        .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
        .FirstOrDefault(line => line.TrimStart().StartsWith(busId, StringComparison.OrdinalIgnoreCase))
        ?? string.Empty;

    private static async Task<bool> EnsureUsbAttachedAsync(
        string label,
        UsbDevice device,
        ICollection<EnvironmentGuideStep> steps,
        CancellationToken cancellationToken)
    {
        var list = await RunAsync("usbipd", ["list"], cancellationToken);
        var stateLine = FindBusLine(list.Output, device.BusId);
        if (!stateLine.Contains("Shared", StringComparison.OrdinalIgnoreCase)
            && !stateLine.Contains("Attached", StringComparison.OrdinalIgnoreCase))
        {
            var bind = await RunElevatedUsbIpdAsync(["bind", "--busid", device.BusId], cancellationToken);
            steps.Add(Step($"{label} bind", bind.Success,
                bind.Success ? $"busid {device.BusId} 已共享" : bind.Error, bind.Command));
            if (!bind.Success)
            {
                return false;
            }
        }
        else
        {
            steps.Add(Step($"{label} bind", true, stateLine.Trim(), $"usbipd bind --busid {device.BusId}"));
        }

        list = await RunAsync("usbipd", ["list"], cancellationToken);
        stateLine = FindBusLine(list.Output, device.BusId);
        if (!stateLine.Contains("Attached", StringComparison.OrdinalIgnoreCase))
        {
            var attach = await RunAsync(
                "usbipd",
                ["attach", "--wsl", "--busid", device.BusId],
                cancellationToken);
            steps.Add(Step($"{label} attach", attach.Success,
                attach.Success ? $"busid {device.BusId} 已挂载" : attach.Error, attach.Command));
            return attach.Success;
        }

        steps.Add(Step($"{label} attach", true, stateLine.Trim(),
            $"usbipd attach --wsl --busid {device.BusId}"));
        return true;
    }

    private static Task<ProcessResult> RunWslAsync(
        TerminalSettings settings,
        string command,
        CancellationToken cancellationToken) => RunAsync(
            "wsl.exe",
            BuildWslArguments(settings, "bash", "-lc", command),
            cancellationToken);

    private static IReadOnlyList<string> BuildWslArguments(
        TerminalSettings settings,
        params string[] command)
    {
        var arguments = new List<string> { "-d", settings.WslDistribution };
        if (!string.IsNullOrWhiteSpace(settings.WslUser))
        {
            arguments.Add("-u");
            arguments.Add(settings.WslUser);
        }
        arguments.Add("--");
        arguments.AddRange(command);
        return arguments;
    }

    private static string DescribeTarget(string targetSerial) =>
        string.IsNullOrWhiteSpace(targetSerial) ? ArmVidPid : $"{ArmVidPid} / {targetSerial}";

    private static async Task<ProcessResult> RunElevatedUsbIpdAsync(
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken)
    {
        var quotedArguments = string.Join(' ', arguments.Select(Quote));
        var command = $"usbipd {quotedArguments}";
        var startInfo = new ProcessStartInfo
        {
            FileName = "usbipd",
            UseShellExecute = true,
            Verb = "runas",
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }
        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                return new ProcessResult(false, string.Empty, "无法启动提权进程", command);
            }
            await process.WaitForExitAsync(cancellationToken);
            return new ProcessResult(process.ExitCode == 0, string.Empty,
                process.ExitCode == 0 ? string.Empty : $"退出码 {process.ExitCode}", command);
        }
        catch (Exception exception)
        {
            return new ProcessResult(false, string.Empty, $"UAC 已取消或执行失败：{exception.Message}", command);
        }
    }

    private static async Task<ProcessResult> RunAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken,
        Encoding? outputEncoding = null)
    {
        var command = $"{fileName} {string.Join(' ', arguments.Select(Quote))}";
        var startInfo = new ProcessStartInfo
        {
            FileName = fileName,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        if (outputEncoding is not null)
        {
            startInfo.StandardOutputEncoding = outputEncoding;
            startInfo.StandardErrorEncoding = outputEncoding;
        }
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }
        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                return new ProcessResult(false, string.Empty, "进程启动失败", command);
            }
            var outputTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
            var errorTask = process.StandardError.ReadToEndAsync(cancellationToken);
            await process.WaitForExitAsync(cancellationToken);
            var output = await outputTask;
            var error = await errorTask;
            return new ProcessResult(process.ExitCode == 0, output, error.Trim(), command);
        }
        catch (Exception exception)
        {
            return new ProcessResult(false, string.Empty, exception.Message, command);
        }
    }

    private static string Quote(string value) => value.Any(char.IsWhiteSpace) ? $"\"{value.Replace("\"", "\\\"")}\"" : value;

    internal sealed record UsbDevice(string BusId, string InstanceId);

    private sealed record ProcessResult(bool Success, string Output, string Error, string Command);
}
