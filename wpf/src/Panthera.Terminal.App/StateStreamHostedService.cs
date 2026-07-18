using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

public sealed class StateStreamHostedService : BackgroundService
{
    private readonly IArmdClient _client;
    private readonly LatestStateSlot<RobotSnapshot> _slot;
    private readonly ILogger<StateStreamHostedService> _logger;

    public StateStreamHostedService(
        IArmdClient client,
        LatestStateSlot<RobotSnapshot> slot,
        ILogger<StateStreamHostedService> logger)
    {
        _client = client;
        _slot = slot;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Delay(TimeSpan.FromSeconds(3), stoppingToken);
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await foreach (var snapshot in _client.StreamStateAsync(60, stoppingToken))
                {
                    _slot.Publish(snapshot);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception)
            {
                _logger.LogWarning(exception, "状态流断开，稍后重连");
                await Task.Delay(TimeSpan.FromSeconds(1), stoppingToken);
            }
        }
    }
}
