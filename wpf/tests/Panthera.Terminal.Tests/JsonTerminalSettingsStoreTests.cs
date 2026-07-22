using Panthera.Terminal.Core;
using Panthera.Terminal.Settings;

namespace Panthera.Terminal.Tests;

public sealed class JsonTerminalSettingsStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), $"panthera-settings-{Guid.NewGuid():N}");

    [Fact]
    public void SaveAndLoad_RoundTripsSettings()
    {
        var path = Path.Combine(_directory, "settings.json");
        var store = new JsonTerminalSettingsStore(path);
        var expected = new TerminalSettings(
            Endpoint: "http://100.78.118.74:50051",
            CameraEndpoint: "http://100.78.118.74:50052",
            Theme: "Dark",
            JogSpeed: 0.22,
            WslDistribution: "Ubuntu",
            BackendMode: "Remote");

        store.Save(expected);
        var actual = store.Load();

        Assert.Equal(expected, actual);
    }

    [Fact]
    public void Load_CorruptJson_ReturnsDefaults()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "settings.json");
        File.WriteAllText(path, "not-json");

        var actual = new JsonTerminalSettingsStore(path).Load();

        Assert.Equal(new TerminalSettings(), actual);
    }

    [Theory]
    [InlineData("WslBridge", true)]
    [InlineData("Remote", false)]
    [InlineData("remote", false)]
    public void UsesWslBridge_RecognizesRemoteMode(string mode, bool expected)
    {
        Assert.Equal(expected, new TerminalSettings(BackendMode: mode).UsesWslBridge);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
