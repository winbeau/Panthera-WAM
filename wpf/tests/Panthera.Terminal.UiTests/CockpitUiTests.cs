using System.Diagnostics;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Input;
using FlaUI.Core.Tools;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;
using Xunit;

namespace Panthera.Terminal.UiTests;

public sealed class CockpitUiTests
{
    [Fact]
    public void Keyboard_navigation_reaches_every_v1_control_and_cycles()
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
        startInfo.Environment["PANTHERA_UI_ACCEPTANCE"] = "1";
        startInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = "HighContrast";

        using var application = Application.Launch(startInfo);
        using var automation = new UIA3Automation();
        try
        {
            var window = Retry.WhileNull(
                () => application.GetMainWindow(automation),
                TimeSpan.FromSeconds(15),
                TimeSpan.FromMilliseconds(200)).Result;
            Assert.NotNull(window);

            var acquire = window.FindFirstDescendant(condition => condition.ByAutomationId("AcquireControlButton"));
            Assert.NotNull(acquire);
            Assert.True(Retry.WhileFalse(
                () => acquire.IsEnabled,
                TimeSpan.FromSeconds(10),
                TimeSpan.FromMilliseconds(100)).Success);
            Assert.True(acquire.Properties.IsKeyboardFocusable.Value);
            acquire.Focus();
            Assert.Equal("AcquireControlButton", automation.FocusedElement().AutomationId);
            acquire.AsButton().Invoke();

            string[] expectedIds =
            [
                "ReleaseControlButton",
                "ThemeSelector",
                "ResetEStopButton",
                "EStopButton",
                "MoveJButton",
                "MoveLButton",
                "CancelExecutionButton",
                "GripperOpenButton",
                "GripperCloseButton",
                "J1NegativeJogButton",
                "J1PositiveJogButton",
                "J2NegativeJogButton",
                "J2PositiveJogButton",
                "J3NegativeJogButton",
                "J3PositiveJogButton",
                "J4NegativeJogButton",
                "J4PositiveJogButton",
                "J5NegativeJogButton",
                "J5PositiveJogButton",
                "J6NegativeJogButton",
                "J6PositiveJogButton",
            ];
            var expected = expectedIds.ToDictionary(
                id => id,
                id => window.FindFirstDescendant(condition => condition.ByAutomationId(id)));
            Assert.DoesNotContain(expected, pair => pair.Value is null);
            Assert.True(Retry.WhileFalse(
                () => expected.Values.All(element => element?.IsEnabled == true),
                TimeSpan.FromSeconds(10),
                TimeSpan.FromMilliseconds(100)).Success);
            foreach (var (id, element) in expected)
            {
                Assert.True(element!.Properties.IsKeyboardFocusable.Value, $"{id} is not keyboard focusable");
            }

            var release = expected["ReleaseControlButton"]!;
            release.Focus();
            var seen = new HashSet<string>(StringComparer.Ordinal) { release.AutomationId };
            var cycled = false;
            for (var index = 0; index < 120; index++)
            {
                Keyboard.Press(VirtualKeyShort.TAB);
                Thread.Sleep(30);
                var focusedId = automation.FocusedElement().AutomationId;
                if (!string.IsNullOrWhiteSpace(focusedId))
                {
                    seen.Add(focusedId);
                }
                if (focusedId == release.AutomationId)
                {
                    cycled = true;
                    break;
                }
            }

            Assert.True(cycled, "Tab focus did not cycle back to the release-control button");
            var missing = expectedIds.Where(id => !seen.Contains(id)).ToArray();
            Assert.Empty(missing);
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

    [Theory]
    [InlineData("System")]
    [InlineData("Light")]
    [InlineData("Dark")]
    [InlineData("HighContrast")]
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
        var artifactDirectory = Environment.GetEnvironmentVariable("PANTHERA_UI_ARTIFACTS")
            ?? Path.Combine(AppContext.BaseDirectory, "ui-artifacts");
        Directory.CreateDirectory(artifactDirectory);
        var screenshotPath = Path.Combine(
            artifactDirectory,
            $"cockpit-{theme.ToLowerInvariant()}.png");

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
        }
        finally
        {
            application.Close();
            if (!application.HasExited)
            {
                application.Kill();
            }
        }

        var screenshotInfo = new ProcessStartInfo(dotnetHost)
        {
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        screenshotInfo.ArgumentList.Add(applicationAssembly);
        screenshotInfo.Environment["PANTHERA_UI_TEST"] = "1";
        screenshotInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = theme;
        screenshotInfo.Environment["PANTHERA_SCREENSHOT_PATH"] = screenshotPath;
        using var screenshotProcess = Process.Start(screenshotInfo)
            ?? throw new InvalidOperationException("WPF screenshot process did not start");
        Assert.True(screenshotProcess.WaitForExit(30_000), "WPF screenshot process did not exit");
        Assert.Equal(0, screenshotProcess.ExitCode);
        Assert.True(File.Exists(screenshotPath), $"Screenshot was not written to {screenshotPath}");
    }
}
