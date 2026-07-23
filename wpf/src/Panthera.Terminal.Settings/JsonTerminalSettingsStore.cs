using System.Text.Json;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Settings;

public sealed class JsonTerminalSettingsStore : ITerminalSettingsStore
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };
    private readonly string _path;

    public JsonTerminalSettingsStore(string? path = null)
    {
        var directory = path is null
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Panthera")
            : Path.GetDirectoryName(path) ?? throw new ArgumentException("配置路径必须包含目录", nameof(path));
        Directory.CreateDirectory(directory);
        _path = path ?? Path.Combine(directory, "terminal-settings.json");
    }

    public TerminalSettings Load()
    {
        if (!File.Exists(_path))
        {
            return new TerminalSettings();
        }

        try
        {
            var json = File.ReadAllText(_path);
            var settings = JsonSerializer.Deserialize<TerminalSettings>(json)
                ?? new TerminalSettings();
            using var document = JsonDocument.Parse(json);
            if (!document.RootElement.TryGetProperty(
                    nameof(TerminalSettings.OverheadCameraEndpoint),
                    out _))
            {
                settings = settings with
                {
                    OverheadCameraEndpoint = LegacyOverheadEndpoint(settings),
                };
            }
            return settings;
        }
        catch (JsonException)
        {
            return new TerminalSettings();
        }
    }

    public void Save(TerminalSettings settings) =>
        File.WriteAllText(_path, JsonSerializer.Serialize(settings, Options));

    private static string LegacyOverheadEndpoint(TerminalSettings settings)
    {
        if (settings.UsesWslBridge || settings.UsesSshTunnel)
        {
            return "http://127.0.0.1:50048";
        }
        if (!Uri.TryCreate(settings.CameraEndpoint, UriKind.Absolute, out var cameraUri))
        {
            return "http://127.0.0.1:50048";
        }
        var builder = new UriBuilder(cameraUri) { Port = 50053 };
        return builder.Uri.GetLeftPart(UriPartial.Authority);
    }
}
