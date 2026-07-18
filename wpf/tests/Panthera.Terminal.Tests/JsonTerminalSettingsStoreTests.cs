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
        var expected = new TerminalSettings(Theme: "Dark", JogSpeed: 0.22, WslDistribution: "Ubuntu");

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

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
