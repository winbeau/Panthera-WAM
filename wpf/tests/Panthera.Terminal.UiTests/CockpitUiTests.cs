using System.Diagnostics;
using FlaUI.Core;
using FlaUI.Core.Capturing;
using FlaUI.Core.Tools;
using FlaUI.UIA3;
using Xunit;

namespace Panthera.Terminal.UiTests;

public sealed class CockpitUiTests
{
    [Theory]
    [InlineData("System")]
    [InlineData("Light")]
    [InlineData("Dark")]
    public void Cockpit_exposes_the_safety_and_control_surface(string theme)
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_RUN_UI_TESTS") != "1")
        {
            return;
        }

        var applicationAssembly = Path.Combine(AppContext.BaseDirectory, "Panthera.Terminal.App.dll");
        Assert.True(File.Exists(applicationAssembly), $"WPF app assembly was not copied to {applicationAssembly}");

        var dotnetRoot = Environment.GetEnvironmentVariable("DOTNET_ROOT")
            ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".dotnet");
        var dotnetHost = Path.Combine(dotnetRoot, "dotnet.exe");
        Assert.True(File.Exists(dotnetHost), $".NET host was not found at {dotnetHost}");

        var startInfo = new ProcessStartInfo(dotnetHost)
        {
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        startInfo.ArgumentList.Add(applicationAssembly);
        startInfo.Environment["PANTHERA_UI_TEST"] = "1";
        startInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = theme;

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
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("ResetEStopButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("MoveLButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("CancelExecutionButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("GripperOpenButton")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("GripperCloseButton")));

            var artifactDirectory = Environment.GetEnvironmentVariable("PANTHERA_UI_ARTIFACTS")
                ?? Path.Combine(AppContext.BaseDirectory, "ui-artifacts");
            Directory.CreateDirectory(artifactDirectory);
            Capture.Element(window).ToFile(
                Path.Combine(artifactDirectory, $"cockpit-{theme.ToLowerInvariant()}.png"));
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
