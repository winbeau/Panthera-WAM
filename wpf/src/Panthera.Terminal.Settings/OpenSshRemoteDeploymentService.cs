using System.Diagnostics;
using System.Text;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Settings;

/// <summary>
/// Uses the user's OpenSSH installation to probe and start an already deployed
/// Panthera-WAM workspace. It deliberately does not install packages or copy
/// files: the remote repository and its service units/launcher must already be
/// present.
/// </summary>
public sealed class OpenSshRemoteDeploymentService : IRemoteDeploymentService
{
    private const string ProbeMarker = "PANTHERA_SSH_PROBE_V1";
    private readonly TimeSpan _commandTimeout;

    public OpenSshRemoteDeploymentService(TimeSpan? commandTimeout = null)
    {
        _commandTimeout = commandTimeout ?? TimeSpan.FromSeconds(90);
    }

    public async Task<RemoteDeploymentReport> ConfigureAndStartAsync(
        SshConnectionSettings settings,
        CancellationToken cancellationToken = default)
    {
        if (!settings.IsConfigured)
        {
            return Failure("SSH 参数不完整：需要主机、端口和用户名", "SSH 连接");
        }

        ProcessResult probe;
        try
        {
            probe = await RunSshScriptAsync(settings, BuildProbeScript(), cancellationToken);
        }
        catch (Exception exception)
        {
            return Failure($"SSH 探测失败：{exception.Message}", "SSH 连接");
        }

        if (!probe.Success)
        {
            return Failure(
                string.IsNullOrWhiteSpace(probe.Error) ? "远程探测失败" : probe.Error.Trim(),
                "SSH 连接");
        }

        var values = ParseProbeOutput(probe.Output);
        if (!string.Equals(Get(values, "marker", string.Empty), ProbeMarker, StringComparison.Ordinal))
        {
            return Failure("远程输出缺少 Panthera 探测标记，未继续执行启动操作", "SSH 探测");
        }
        var steps = new List<EnvironmentGuideStep>
        {
            new("SSH 连接", true, $"已连接 {settings.User}@{settings.Host}:{settings.Port}", probe.Command),
        };

        var architecture = Get(values, "arch", "unknown");
        var targetKind = Get(values, "target_kind", "unknown");
        var repository = Get(values, "repo", string.Empty);
        var startMethod = Get(values, "start_method", "none");
        steps.Add(new EnvironmentGuideStep(
            "远程系统识别",
            architecture != "unknown",
            $"{targetKind} · {Get(values, "kernel", "unknown")} · {architecture} · {Get(values, "os", "unknown")}",
            "uname -s; uname -m; /etc/os-release"));

        if (string.IsNullOrWhiteSpace(repository))
        {
            steps.Add(new EnvironmentGuideStep(
                "Panthera 工作目录",
                false,
                "未找到已部署的 Panthera-WAM 工作区；未创建或下载任何目录",
                "探测 $HOME 下的 pyproject.toml / armd / deploy"));
            return new RemoteDeploymentReport(steps, targetKind, architecture, string.Empty, startMethod);
        }

        steps.Add(new EnvironmentGuideStep(
            "Panthera 工作目录",
            true,
            repository,
            $"探测到 {repository}"));

        if (!string.Equals(startMethod, "systemd-user", StringComparison.OrdinalIgnoreCase)
            && !string.Equals(startMethod, "launcher", StringComparison.OrdinalIgnoreCase))
        {
            steps.Add(new EnvironmentGuideStep(
                "启动脚本识别",
                false,
                "未找到可用的 systemd user service 或已部署启动脚本；未执行安装操作",
                "systemctl --user cat armd.service camerad.service / deploy/panthera-up.zsh"));
            return new RemoteDeploymentReport(steps, targetKind, architecture, repository, startMethod);
        }

        steps.Add(new EnvironmentGuideStep(
            "启动脚本识别",
            true,
            string.Equals(startMethod, "systemd-user", StringComparison.OrdinalIgnoreCase)
                ? "使用已部署的 systemd user service"
                : Get(values, "launcher", "deploy/panthera-up.zsh"),
            startMethod));

        ProcessResult start;
        try
        {
            start = await RunSshScriptAsync(settings, BuildStartScript(repository, startMethod), cancellationToken);
        }
        catch (Exception exception)
        {
            steps.Add(new EnvironmentGuideStep("启动 Linux 后端", false, exception.Message, startMethod));
            return new RemoteDeploymentReport(steps, targetKind, architecture, repository, startMethod);
        }

        steps.Add(new EnvironmentGuideStep(
            "启动 Linux 后端",
            start.Success,
            start.Success ? "armd 与 camerad 已请求启动" : FailureDetail(start, "远程启动命令失败"),
            start.Command));
        if (!start.Success)
        {
            return new RemoteDeploymentReport(steps, targetKind, architecture, repository, startMethod);
        }

        ProcessResult verification;
        try
        {
            verification = await RunSshScriptAsync(settings, BuildVerifyScript(), cancellationToken);
        }
        catch (Exception exception)
        {
            steps.Add(new EnvironmentGuideStep("后端端口探活", false, exception.Message, "ss -ltn"));
            return new RemoteDeploymentReport(steps, targetKind, architecture, repository, startMethod);
        }
        steps.Add(new EnvironmentGuideStep(
            "后端端口探活",
            verification.Success && HasListeningPorts(verification.Output),
            verification.Success
                ? (HasListeningPorts(verification.Output) ? "已发现 50051/50052 监听" : verification.Output.Trim())
                : FailureDetail(verification, "远程端口探活失败"),
            verification.Command));

        return new RemoteDeploymentReport(steps, targetKind, architecture, repository, startMethod);
    }

    /// <summary>Builds the persistent SSH local-forward command used by WPF after restart.</summary>
    public static IReadOnlyList<string> BuildTunnelArguments(
        SshConnectionSettings settings,
        int localArmPort = 50050,
        int localCameraPort = 50049)
    {
        ValidateSettings(settings);
        if (localArmPort is <= 0 or > 65535 || localCameraPort is <= 0 or > 65535)
        {
            throw new ArgumentOutOfRangeException(nameof(localArmPort));
        }

        var args = BaseSshArguments(settings);
        args.Insert(0, "-N");
        args.Insert(1, "-T");
        args.Add("-o");
        args.Add("ExitOnForwardFailure=yes");
        args.Add("-L");
        args.Add($"127.0.0.1:{localArmPort}:127.0.0.1:50051");
        args.Add("-L");
        args.Add($"127.0.0.1:{localCameraPort}:127.0.0.1:50052");
        args.Add(Target(settings));
        return args;
    }

    internal static string BuildProbeScript() => """
        set -eu
        marker='PANTHERA_SSH_PROBE_V1'
        b64() { printf %s "$1" | base64 | tr -d '\n'; }
        emit() { printf '%s\t%s\n' "$1" "$(b64 "$2")"; }
        emit marker "$marker"
        emit kernel "$(uname -s 2>/dev/null || printf unknown)"
        emit arch "$(uname -m 2>/dev/null || printf unknown)"
        emit os "$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-${ID:-unknown}}" || printf unknown)"
        if grep -qi microsoft /proc/version 2>/dev/null || [ -n "${WSL_INTEROP:-}" ]; then
          target_kind='WSL'
        elif [ "$(uname -m 2>/dev/null || printf unknown)" = 'aarch64' ] || [ "$(uname -m 2>/dev/null || printf unknown)" = 'arm64' ]; then
          target_kind='RaspberryPi/Linux ARM64'
        else
          target_kind='Linux'
        fi
        emit target_kind "$target_kind"
        repo=''
        for candidate in "$HOME/Panthera-WAM" "$HOME/Panthera-WAM-v2" "$HOME/panthera-wam"; do
          if [ -f "$candidate/pyproject.toml" ] && [ -d "$candidate/armd" ] && [ -d "$candidate/deploy" ]; then
            repo="$candidate"
            break
          fi
        done
        if [ -z "$repo" ] && command -v find >/dev/null 2>&1; then
          manifest=$(find "$HOME" -maxdepth 4 -type f -path '*/armd/pyproject.toml' -print -quit 2>/dev/null || true)
          if [ -n "$manifest" ]; then
            repo=$(dirname "$(dirname "$manifest")")
          fi
        fi
        emit repo "$repo"
        start_method='none'
        if systemctl --user cat armd.service >/dev/null 2>&1 && systemctl --user cat camerad.service >/dev/null 2>&1; then
          start_method='systemd-user'
        elif [ "$target_kind" = 'WSL' ] && [ -n "$repo" ] \
          && [ -f "$repo/deploy/panthera-up.zsh" ] && command -v zsh >/dev/null 2>&1 \
          && [ -f "$repo/vendor/Panthera-HT_SDK/panthera_python/robot_param/Follower.yaml" ] \
          && { command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; } \
          && [ -x "$repo/.venv/bin/python" ] && [ -x "$repo/.venv/bin/armd" ] \
          && [ -x "$repo/.venv/bin/camerad" ] && [ -x "$repo/.venv/bin/panthera" ] \
          && [ "$("$repo/.venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)" = '3.11' ] \
          && "$repo/.venv/bin/python" -c 'import hightorque_robot; from pyrealsense2 import pyrealsense2 as rs; assert rs.__version__ == "2.58.1"' >/dev/null 2>&1; then
            start_method='launcher'
            emit launcher "$repo/deploy/panthera-up.zsh"
        fi
        emit start_method "$start_method"
        """;

    internal static string BuildStartScript(string repository, string startMethod)
    {
        var quotedRepository = QuotePosix(repository);
        return string.Equals(startMethod, "systemd-user", StringComparison.OrdinalIgnoreCase)
            ? $"set -eu\ncd {quotedRepository}\nsystemctl --user start camerad.service armd.service\nsystemctl --user is-active camerad.service\nsystemctl --user is-active armd.service\n"
            : $$"""
                set -eu
                export PANTHERA_REPO={{quotedRepository}}
                cd {{quotedRepository}}
                zsh -lc 'source "$PANTHERA_REPO/deploy/panthera-up.zsh"; panthera-up'
                """;
    }

    internal static string BuildVerifyScript() => "set -eu\nss -ltn 2>/dev/null | grep -E ':(50051|50052)([[:space:]]|$)' || true\n";

    internal static Dictionary<string, string> ParseProbeOutput(string output)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in output.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = line.IndexOf('\t');
            if (separator <= 0)
            {
                continue;
            }
            var key = line[..separator];
            var encoded = line[(separator + 1)..];
            try
            {
                values[key] = Encoding.UTF8.GetString(Convert.FromBase64String(encoded));
            }
            catch (FormatException)
            {
                values[key] = string.Empty;
            }
        }
        return values;
    }

    internal static bool HasListeningPorts(string output) =>
        output.Contains(":50051", StringComparison.Ordinal)
        && output.Contains(":50052", StringComparison.Ordinal);

    private async Task<ProcessResult> RunSshScriptAsync(
        SshConnectionSettings settings,
        string script,
        CancellationToken cancellationToken)
    {
        ValidateSettings(settings);
        var encodedScript = Convert.ToBase64String(Encoding.UTF8.GetBytes(script));
        var remoteCommand = $"printf %s {QuotePosix(encodedScript)} | base64 -d | sh";
        var args = BaseSshArguments(settings);
        args.Add(Target(settings));
        args.Add(remoteCommand);
        return await RunProcessAsync("ssh.exe", args, cancellationToken);
    }

    private async Task<ProcessResult> RunProcessAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken)
    {
        var command = $"{fileName} {string.Join(' ', arguments.Select(QuoteForDisplay))}";
        var startInfo = new ProcessStartInfo
        {
            FileName = fileName,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        using var process = Process.Start(startInfo);
        if (process is null)
        {
            return new ProcessResult(false, string.Empty, "无法启动 ssh.exe", command);
        }
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(_commandTimeout);
        try
        {
            var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
            var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
            await process.WaitForExitAsync(timeout.Token);
            return new ProcessResult(process.ExitCode == 0, await stdout, await stderr, command);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            try { process.Kill(entireProcessTree: true); } catch { /* best effort */ }
            return new ProcessResult(false, string.Empty, "ssh 命令超时", command);
        }
    }

    private static List<string> BaseSshArguments(SshConnectionSettings settings)
    {
        ValidateSettings(settings);
        var args = new List<string>
        {
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1",
            "-o", $"StrictHostKeyChecking={(settings.AcceptNewHostKey ? "accept-new" : "yes")}",
            "-o", "LogLevel=ERROR",
            "-p", settings.Port.ToString(System.Globalization.CultureInfo.InvariantCulture),
        };
        if (!string.IsNullOrWhiteSpace(settings.IdentityFile))
        {
            args.Add("-i");
            args.Add(Path.GetFullPath(settings.IdentityFile));
        }
        return args;
    }

    private static string Target(SshConnectionSettings settings) => $"{settings.User}@{settings.Host}";

    private static void ValidateSettings(SshConnectionSettings settings)
    {
        if (!settings.IsConfigured)
        {
            throw new ArgumentException("SSH 参数不完整", nameof(settings));
        }
        if (settings.Host.Any(char.IsWhiteSpace) || settings.Host.StartsWith("-", StringComparison.Ordinal))
        {
            throw new ArgumentException("SSH 主机名不能包含空白或以 - 开头", nameof(settings));
        }
        if (settings.User.Any(char.IsWhiteSpace) || settings.User.Contains('@'))
        {
            throw new ArgumentException("SSH 用户名格式无效", nameof(settings));
        }
    }

    private static RemoteDeploymentReport Failure(string detail, string name) =>
        new([new EnvironmentGuideStep(name, false, detail, "ssh")]);

    private static string Get(IReadOnlyDictionary<string, string> values, string key, string fallback) =>
        values.TryGetValue(key, out var value) && !string.IsNullOrWhiteSpace(value) ? value : fallback;

    private static string FailureDetail(ProcessResult result, string fallback) =>
        !string.IsNullOrWhiteSpace(result.Error)
            ? result.Error.Trim()
            : !string.IsNullOrWhiteSpace(result.Output) ? result.Output.Trim() : fallback;

    private static string QuotePosix(string value) => $"'{value.Replace("'", "'\\''", StringComparison.Ordinal)}'";

    private static string QuoteForDisplay(string value) => value.Contains(' ') ? QuotePosix(value) : value;

    private sealed record ProcessResult(bool Success, string Output, string Error, string Command);
}
