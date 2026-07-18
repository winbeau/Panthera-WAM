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
            return JsonSerializer.Deserialize<TerminalSettings>(File.ReadAllText(_path))
                ?? new TerminalSettings();
        }
        catch (JsonException)
        {
            return new TerminalSettings();
        }
    }

    public void Save(TerminalSettings settings) =>
        File.WriteAllText(_path, JsonSerializer.Serialize(settings, Options));
}
