using Microsoft.Extensions.Hosting;
using Panthera.Terminal.Core;
using Panthera.Terminal.Grpc;

namespace Panthera.Terminal.App;

public sealed class WslTcpBridgeHostedService : BackgroundService
{
    private readonly WslTcpBridge _armBridge;
    private readonly WslTcpBridge _cameraBridge;

    public WslTcpBridgeHostedService(TerminalSettings settings)
    {
        _armBridge = new WslTcpBridge(
            settings.WslDistribution,
            settings.WslUser,
            EndpointPort(settings.Endpoint, 50050),
            50051);
        _cameraBridge = new WslTcpBridge(
            settings.WslDistribution,
            settings.WslUser,
            EndpointPort(settings.CameraEndpoint, 50049),
            50052);
    }

    protected override Task ExecuteAsync(CancellationToken stoppingToken) =>
        Task.WhenAll(
            _armBridge.RunAsync(stoppingToken),
            _cameraBridge.RunAsync(stoppingToken));

    private static int EndpointPort(string endpoint, int fallback) =>
        Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) ? uri.Port : fallback;
}
