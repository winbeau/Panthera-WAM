using System.Diagnostics;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Panthera.Terminal.Core;
using Panthera.Terminal.Settings;

namespace Panthera.Terminal.App;

/// <summary>
/// Keeps localhost:50050/50049 forwarded to the selected Linux backend. The
/// remote service can therefore bind only to loopback on either Raspberry Pi or
/// WSL; WPF does not need to guess the target network interface.
/// </summary>
public sealed class SshTunnelHostedService : BackgroundService
{
    private readonly TerminalSettings _settings;
    private readonly ILogger<SshTunnelHostedService> _logger;
    private Process? _process;

    public SshTunnelHostedService(
        TerminalSettings settings,
        ILogger<SshTunnelHostedService> logger)
    {
        _settings = settings;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!_settings.UsesSshTunnel || !_settings.SshSettings.IsConfigured)
        {
            return;
        }

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                _process = StartTunnel(_settings.SshSettings);
                var stderrTask = _process.StandardError.ReadToEndAsync(stoppingToken);
                await _process.WaitForExitAsync(stoppingToken);
                var stderr = await stderrTask;
                if (!stoppingToken.IsCancellationRequested)
                {
                    _logger.LogWarning(
                        "SSH tunnel exited with code {ExitCode}: {Error}",
                        _process.ExitCode,
                        stderr.Trim());
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "SSH tunnel failed to start");
            }
            finally
            {
                DisposeTunnel();
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(2), stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        KillTunnel();
        await base.StopAsync(cancellationToken);
    }

    private static Process StartTunnel(SshConnectionSettings settings)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "ssh.exe",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = false,
            RedirectStandardError = true,
        };
        foreach (var argument in OpenSshRemoteDeploymentService.BuildTunnelArguments(settings))
        {
            startInfo.ArgumentList.Add(argument);
        }
        return Process.Start(startInfo) ?? throw new InvalidOperationException("无法启动 ssh.exe 隧道");
    }

    private void KillTunnel()
    {
        var process = Volatile.Read(ref _process);
        if (process is null)
        {
            return;
        }
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            // Best effort during application shutdown.
        }
    }

    private void DisposeTunnel()
    {
        var process = Interlocked.Exchange(ref _process, null);
        if (process is null)
        {
            return;
        }
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch { /* best effort */ }
        process.Dispose();
    }
}
