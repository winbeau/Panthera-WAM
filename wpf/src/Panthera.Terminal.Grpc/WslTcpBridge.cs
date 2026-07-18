using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;

namespace Panthera.Terminal.Grpc;

public sealed class WslTcpBridge
{
    private readonly string _distribution;
    private readonly string _user;
    private readonly int _listenPort;
    private readonly int _targetPort;
    private readonly ConcurrentDictionary<long, Task> _connections = new();
    private long _connectionId;

    public WslTcpBridge(string distribution, string user, int listenPort = 50050, int targetPort = 50051)
    {
        _distribution = distribution;
        _user = user;
        _listenPort = listenPort;
        _targetPort = targetPort;
    }

    public async Task RunAsync(CancellationToken cancellationToken)
    {
        var listener = new TcpListener(IPAddress.Loopback, _listenPort);
        listener.Start();
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                var client = await listener.AcceptTcpClientAsync(cancellationToken);
                var connectionId = Interlocked.Increment(ref _connectionId);
                var task = HandleConnectionAsync(client, cancellationToken);
                _connections[connectionId] = task;
                _ = task.ContinueWith(
                    completedTask => _connections.TryRemove(connectionId, out _),
                    CancellationToken.None,
                    TaskContinuationOptions.ExecuteSynchronously,
                    TaskScheduler.Default);
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        finally
        {
            listener.Stop();
            await Task.WhenAll(_connections.Values);
        }
    }

    private async Task HandleConnectionAsync(TcpClient client, CancellationToken cancellationToken)
    {
        using (client)
        using (var process = StartRelayProcess())
        using (var connectionLifetime = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
        {
            var network = client.GetStream();
            var toWsl = network.CopyToAsync(process.StandardInput.BaseStream, connectionLifetime.Token);
            var fromWsl = process.StandardOutput.BaseStream.CopyToAsync(network, connectionLifetime.Token);
            var errorDrain = process.StandardError.ReadToEndAsync(connectionLifetime.Token);
            var processExit = process.WaitForExitAsync(connectionLifetime.Token);
            await Task.WhenAny(toWsl, fromWsl, processExit);
            connectionLifetime.Cancel();
            try
            {
                client.Close();
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
            }
            catch (InvalidOperationException)
            {
            }
            await ObserveCompletionAsync(toWsl);
            await ObserveCompletionAsync(fromWsl);
            await ObserveCompletionAsync(errorDrain);
            await ObserveCompletionAsync(processExit);
        }
    }

    private Process StartRelayProcess()
    {
        var startInfo = new ProcessStartInfo("wsl.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        startInfo.ArgumentList.Add("-d");
        startInfo.ArgumentList.Add(_distribution);
        if (!string.IsNullOrWhiteSpace(_user))
        {
            startInfo.ArgumentList.Add("-u");
            startInfo.ArgumentList.Add(_user);
        }
        startInfo.ArgumentList.Add("--");
        startInfo.ArgumentList.Add("nc");
        startInfo.ArgumentList.Add("127.0.0.1");
        startInfo.ArgumentList.Add(_targetPort.ToString());
        return Process.Start(startInfo) ?? throw new InvalidOperationException("无法启动 WSL TCP 桥");
    }

    private static async Task ObserveCompletionAsync(Task task)
    {
        try
        {
            await task;
        }
        catch (Exception exception) when (
            exception is OperationCanceledException or IOException or ObjectDisposedException)
        {
        }
    }
}
