using Microsoft.Extensions.Hosting;
using Panthera.Terminal.Core;
using Panthera.Terminal.Grpc;

namespace Panthera.Terminal.App;

public sealed class WslTcpBridgeHostedService : BackgroundService
{
    private readonly WslTcpBridge _bridge;

    public WslTcpBridgeHostedService(TerminalSettings settings)
    {
        _bridge = new WslTcpBridge(settings.WslDistribution, settings.WslUser);
    }

    protected override Task ExecuteAsync(CancellationToken stoppingToken) =>
        _bridge.RunAsync(stoppingToken);
}
