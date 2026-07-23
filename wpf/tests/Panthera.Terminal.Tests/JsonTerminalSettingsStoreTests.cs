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
            OverheadCameraEndpoint: "http://100.78.118.74:50053",
            Theme: "Dark",
            JogSpeed: 0.22,
            WslDistribution: "Ubuntu",
            BackendMode: "SshRemote",
            Ssh: new SshConnectionSettings(
                Host: "pi5",
                Port: 2222,
                User: "winbeau",
                IdentityFile: @"C:\Users\genev\.ssh\id_ed25519"));

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

    [Fact]
    public void Load_LegacySettings_FillsOverheadEndpointDefault()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "legacy-settings.json");
        File.WriteAllText(path, """{"Endpoint":"http://pi5:50051","CameraEndpoint":"http://pi5:50052"}""");

        var actual = new JsonTerminalSettingsStore(path).Load();

        Assert.Equal("http://pi5:50051", actual.Endpoint);
        Assert.Equal("http://pi5:50052", actual.CameraEndpoint);
        Assert.Equal("http://127.0.0.1:50048", actual.OverheadCameraEndpoint);
    }

    [Fact]
    public void Load_LegacyRemoteSettings_DerivesOverheadEndpointFromPiHost()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "legacy-remote-settings.json");
        File.WriteAllText(
            path,
            """{"Endpoint":"http://pi5:50051","CameraEndpoint":"http://pi5:50052","BackendMode":"Remote"}""");

        var actual = new JsonTerminalSettingsStore(path).Load();

        Assert.Equal("http://pi5:50053", actual.OverheadCameraEndpoint);
    }

    [Theory]
    [InlineData("WslBridge", true)]
    [InlineData("Remote", false)]
    [InlineData("remote", false)]
    [InlineData("SshRemote", false)]
    public void UsesWslBridge_RecognizesRemoteMode(string mode, bool expected)
    {
        Assert.Equal(expected, new TerminalSettings(BackendMode: mode).UsesWslBridge);
    }

    [Theory]
    [InlineData("SshRemote", true)]
    [InlineData("sshremote", true)]
    [InlineData("Remote", false)]
    [InlineData("WslBridge", false)]
    public void UsesSshTunnel_RecognizesSshMode(string mode, bool expected)
    {
        Assert.Equal(expected, new TerminalSettings(BackendMode: mode).UsesSshTunnel);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
