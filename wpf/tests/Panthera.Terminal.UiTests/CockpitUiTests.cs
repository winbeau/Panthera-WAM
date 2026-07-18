using System.Diagnostics;
using FlaUI.Core;
using FlaUI.Core.Capturing;
using FlaUI.Core.Tools;
using FlaUI.UIA3;
using Xunit;

namespace Panthera.Terminal.UiTests;

public sealed class CockpitUiTests
{
    [Fact]
    public void Cockpit_exposes_the_safety_and_control_surface()
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_RUN_UI_TESTS") != "1")
        {
            return;
        }

        var executable = Path.Combine(AppContext.BaseDirectory, "Panthera.Terminal.App.exe");
        Assert.True(File.Exists(executable), $"WPF app executable was not copied to {executable}");
        var startInfo = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        startInfo.Environment["PANTHERA_UI_TEST"] = "1";
        startInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = "Dark";

        using var application = Application.Launch(startInfo);
        using var automation = new UIA3Automation();
        try
        {
            var window = Retry.WhileNull(
                () => application.GetMainWindow(automation),
                TimeSpan.FromSeconds(15),
                TimeSpan.FromMilliseconds(200)).Result;
            Assert.NotNull(window);
            Assert.Equal("Panthera-HT 控制终端", window.Title);
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("AcquireControlButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("ThemeSelector")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("EStopButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("MoveLButton")));

            var artifactDirectory = Environment.GetEnvironmentVariable("PANTHERA_UI_ARTIFACTS")
                ?? Path.Combine(AppContext.BaseDirectory, "ui-artifacts");
            Directory.CreateDirectory(artifactDirectory);
            Capture.Element(window).ToFile(Path.Combine(artifactDirectory, "cockpit-dark.png"));
        }
        finally
        {
            application.Close();
            if (!application.HasExited)
            {
                application.Kill();
            }
        }
    }
}
