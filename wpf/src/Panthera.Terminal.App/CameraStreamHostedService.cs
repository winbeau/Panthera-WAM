using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

public sealed class CameraStreamHostedService : BackgroundService
{
    private const double CameraStreamRateHz = 30.0;

    private readonly IArmdClient _client;
    private readonly LatestCameraFrames _frames;
    private readonly ILogger<CameraStreamHostedService> _logger;

    public CameraStreamHostedService(
        IArmdClient client,
        LatestCameraFrames frames,
        ILogger<CameraStreamHostedService> logger)
    {
        _client = client;
        _frames = frames;
        _logger = logger;
    }

    protected override Task ExecuteAsync(CancellationToken stoppingToken) =>
        Task.WhenAll(
            PumpAsync(CameraStreamKind.Color, stoppingToken),
            PumpAsync(CameraStreamKind.Depth, stoppingToken));

    private async Task PumpAsync(CameraStreamKind stream, CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await foreach (var frame in _client.StreamCameraFramesAsync(stream, CameraStreamRateHz, stoppingToken))
                {
                    _frames.Publish(frame);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception)
            {
                _logger.LogWarning(exception, "D405 {Stream} 视频流断开，稍后重连", stream);
                await Task.Delay(TimeSpan.FromSeconds(1), stoppingToken);
            }
        }
    }
}
