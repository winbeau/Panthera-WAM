using System.IO;

namespace Panthera.Terminal.App;

internal static class AppDiagnostics
{
    private static readonly object Gate = new();
    private static readonly string DirectoryPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Panthera");

    public static string LogPath
    {
        get
        {
            var overridePath = Environment.GetEnvironmentVariable("PANTHERA_FAILURE_LOG");
            return string.IsNullOrWhiteSpace(overridePath)
                ? Path.Combine(DirectoryPath, "terminal-failures.log")
                : Path.GetFullPath(overridePath);
        }
    }

    public static void Write(string source, Exception exception)
    {
        Write(source, exception.ToString());
    }

    public static void Write(string source, string message)
    {
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath) ?? DirectoryPath);
                File.AppendAllText(
                    LogPath,
                    $"[{DateTimeOffset.Now:O}] {source}{Environment.NewLine}{message}{Environment.NewLine}{Environment.NewLine}");
            }
        }
        catch
        {
        }
    }
}
