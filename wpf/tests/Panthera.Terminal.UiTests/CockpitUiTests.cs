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
    public async Task Real_hardware_wpf_movel_1mm_requires_explicit_confirmation()
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_RUN_REAL_MOVEL_TEST") != "1")
        {
            return;
        }

        const string confirmation = "CONFIRM_WPF_MOVEL_X_PLUS_0.001_M_3_S";
        Assert.Equal(confirmation, Environment.GetEnvironmentVariable("PANTHERA_REAL_CONFIRM"));
        Assert.True(
            int.TryParse(Environment.GetEnvironmentVariable("PANTHERA_WPF_PID"), out var processId),
            "PANTHERA_WPF_PID must identify the already-running real WPF terminal");
        Assert.True(
            double.TryParse(
                Environment.GetEnvironmentVariable("PANTHERA_MOVEL_TARGET_X"),
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture,
                out var targetX),
            "PANTHERA_MOVEL_TARGET_X must be an invariant-culture number");

        Console.WriteLine($"REAL TEST ACTION: acquire control, clear EStop, set X={targetX:R} m, preserve orientation, MoveL over 3.0 s, F12 EStop");
        Console.WriteLine($"SECOND CONFIRMATION: {confirmation}");

        using var application = Application.Attach(processId);
        using var automation = new UIA3Automation();
        var window = Retry.WhileNull(
            () => application.GetMainWindow(automation),
            TimeSpan.FromSeconds(10),
            TimeSpan.FromMilliseconds(100)).Result;
        Assert.NotNull(window);

        try
        {
            var acquire = window.FindFirstDescendant(
                condition => condition.ByAutomationId("AcquireControlButton"));
            var release = window.FindFirstDescendant(
                condition => condition.ByAutomationId("ReleaseControlButton"));
            var resetEStop = window.FindFirstDescendant(
                condition => condition.ByAutomationId("ResetEStopBannerButton"));
            var targetXBox = window.FindFirstDescendant(
                condition => condition.ByAutomationId("MoveLTargetX"));
            var preserveOrientation = window.FindFirstDescendant(
                condition => condition.ByAutomationId("MoveLPreserveOrientationCheckBox"));
            var moveL = window.FindFirstDescendant(
                condition => condition.ByAutomationId("MoveLButton"));
            Assert.NotNull(acquire);
            Assert.NotNull(release);
            Assert.NotNull(resetEStop);
            Assert.NotNull(targetXBox);
            Assert.NotNull(preserveOrientation);
            Assert.NotNull(moveL);

            if (acquire.IsEnabled)
            {
                acquire.AsButton().Invoke();
            }
            Assert.True(Retry.WhileFalse(
                () => release.IsEnabled,
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(50)).Success);

            if (resetEStop.IsEnabled)
            {
                resetEStop.AsButton().Invoke();
            }
            Assert.True(Retry.WhileFalse(
                () => moveL.IsEnabled,
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(50)).Success);

            preserveOrientation.AsCheckBox().IsChecked = true;
            targetXBox.AsTextBox().Text = targetX.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
            targetXBox.Focus();
            Keyboard.Press(VirtualKeyShort.TAB);
            Thread.Sleep(200);

            using var guard = new CancellationTokenSource();
            var guardTask = Task.Run(async () =>
            {
                await Task.Delay(TimeSpan.FromSeconds(5), guard.Token);
                window.Focus();
                Keyboard.Press(VirtualKeyShort.F12);
            }, guard.Token);

            moveL.AsButton().Invoke();
            Assert.True(Retry.WhileFalse(
                () => !moveL.IsEnabled,
                TimeSpan.FromSeconds(1),
                TimeSpan.FromMilliseconds(25)).Success);
            Assert.True(Retry.WhileFalse(
                () => moveL.IsEnabled,
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(50)).Success);
            window.Focus();
            Keyboard.Press(VirtualKeyShort.F12);
            guard.Cancel();
            try
            {
                await guardTask;
            }
            catch (OperationCanceledException)
            {
            }
        }
        finally
        {
            window.Focus();
            Keyboard.Press(VirtualKeyShort.F12);
        }
    }

    [Fact]
    public async Task Real_hardware_wpf_j1_micro_jog_requires_explicit_confirmation()
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_RUN_REAL_UI_TEST") != "1")
        {
            return;
        }

        const string confirmation = "CONFIRM_WPF_J1_0.02_RAD_S_0.5_S";
        Assert.Equal(confirmation, Environment.GetEnvironmentVariable("PANTHERA_REAL_CONFIRM"));
        Assert.True(
            int.TryParse(Environment.GetEnvironmentVariable("PANTHERA_WPF_PID"), out var processId),
            "PANTHERA_WPF_PID must identify the already-running real WPF terminal");

        Console.WriteLine("REAL TEST ACTION: acquire control, clear EStop, set jog speed 0.02 rad/s, hold J1+ for 0.5 s, release, F12 EStop");
        Console.WriteLine($"SECOND CONFIRMATION: {confirmation}");

        using var application = Application.Attach(processId);
        using var automation = new UIA3Automation();
        var window = Retry.WhileNull(
            () => application.GetMainWindow(automation),
            TimeSpan.FromSeconds(10),
            TimeSpan.FromMilliseconds(100)).Result;
        Assert.NotNull(window);

        try
        {
            var acquire = window.FindFirstDescendant(
                condition => condition.ByAutomationId("AcquireControlButton"));
            var release = window.FindFirstDescendant(
                condition => condition.ByAutomationId("ReleaseControlButton"));
            var resetEStop = window.FindFirstDescendant(
                condition => condition.ByAutomationId("ResetEStopBannerButton"));
            var jogSpeed = window.FindFirstDescendant(
                condition => condition.ByAutomationId("JogSpeedSlider"));
            var positiveJog = window.FindFirstDescendant(
                condition => condition.ByAutomationId("J1PositiveJogButton"));
            Assert.NotNull(acquire);
            Assert.NotNull(release);
            Assert.NotNull(resetEStop);
            Assert.NotNull(jogSpeed);
            Assert.NotNull(positiveJog);

            if (acquire.IsEnabled)
            {
                acquire.AsButton().Invoke();
            }
            Assert.True(Retry.WhileFalse(
                () => release.IsEnabled,
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(50)).Success);

            Assert.True(resetEStop.IsEnabled);
            resetEStop.AsButton().Invoke();
            Assert.True(Retry.WhileFalse(
                () => positiveJog.IsEnabled,
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(50)).Success);

            jogSpeed.Focus();
            Keyboard.Press(VirtualKeyShort.HOME);
            Thread.Sleep(200);

            positiveJog.Focus();
            Mouse.MoveTo(positiveJog.BoundingRectangle.Center());
            using var guard = new CancellationTokenSource();
            var guardTask = Task.Run(async () =>
            {
                await Task.Delay(TimeSpan.FromSeconds(1.5), guard.Token);
                window.Focus();
                Keyboard.Press(VirtualKeyShort.F12);
            }, guard.Token);

            Mouse.Down(MouseButton.Left);
            Thread.Sleep(500);
            Mouse.Up(MouseButton.Left);
            window.Focus();
            Keyboard.Press(VirtualKeyShort.F12);
            guard.Cancel();
            try
            {
                await guardTask;
            }
            catch (OperationCanceledException)
            {
            }
        }
        finally
        {
            Mouse.Up(MouseButton.Left);
            window.Focus();
            Keyboard.Press(VirtualKeyShort.F12);
        }
    }

    [Fact]
    public void Hold_to_run_mouse_input_and_f12_reach_the_view_model()
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_RUN_UI_TESTS") != "1")
        {
            return;
        }

        var (application, automation, window, eventLog) = LaunchAcceptanceApp("motion-input");
        using (application)
        using (automation)
        {
            try
            {
                var acquire = window.FindFirstDescendant(
                    condition => condition.ByAutomationId("AcquireControlButton"));
                Assert.NotNull(acquire);
                Assert.True(Retry.WhileFalse(
                    () => acquire.IsEnabled,
                    TimeSpan.FromSeconds(10),
                    TimeSpan.FromMilliseconds(100)).Success);
                acquire.AsButton().Invoke();

                var positiveJog = window.FindFirstDescendant(
                    condition => condition.ByAutomationId("J1PositiveJogButton"));
                Assert.NotNull(positiveJog);
                Assert.True(Retry.WhileFalse(
                    () => positiveJog.IsEnabled,
                    TimeSpan.FromSeconds(10),
                    TimeSpan.FromMilliseconds(100)).Success);

                Mouse.MoveTo(positiveJog.BoundingRectangle.Center());
                Mouse.Down(MouseButton.Left);
                Assert.True(Retry.WhileFalse(
                    () => File.Exists(eventLog) && File.ReadAllText(eventLog).Contains("jog:0:"),
                    TimeSpan.FromSeconds(3),
                    TimeSpan.FromMilliseconds(50)).Success);
                Mouse.Up(MouseButton.Left);

                window.Focus();
                Keyboard.Press(VirtualKeyShort.F12);
                Assert.True(Retry.WhileFalse(
                    () => File.Exists(eventLog) && File.ReadAllText(eventLog).Contains("estop"),
                    TimeSpan.FromSeconds(3),
                    TimeSpan.FromMilliseconds(50)).Success);
            }
            finally
            {
                Mouse.Up(MouseButton.Left);
                CloseApplication(application);
            }
        }
    }

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
        var artifactDirectory = PrepareArtifactDirectory("keyboard");
        startInfo.Environment["PANTHERA_FAILURE_LOG"] = Path.Combine(artifactDirectory, "terminal-failures-keyboard.log");

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
            CloseApplication(application);
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
        startInfo.Environment["PANTHERA_UI_ACCEPTANCE"] = "1";
        startInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = theme;
        var artifactDirectory = PrepareArtifactDirectory(theme.ToLowerInvariant());
        startInfo.Environment["PANTHERA_FAILURE_LOG"] = Path.Combine(
            artifactDirectory,
            $"terminal-failures-{theme.ToLowerInvariant()}.log");
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
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("CadStyleSelector")));
            Assert.NotNull(window.FindFirstDescendant(condition => condition.ByAutomationId("CadCameraSelector")));

            var dataTab = window.FindFirstDescendant(condition => condition.ByAutomationId("DataTabButton"));
            Assert.NotNull(dataTab);
            dataTab.AsRadioButton().IsChecked = true;
            var startTeachSession = Retry.WhileNull(
                () => window.FindFirstDescendant(
                    condition => condition.ByAutomationId("StartTeachSessionButton")),
                TimeSpan.FromSeconds(5),
                TimeSpan.FromMilliseconds(100)).Result;
            Assert.NotNull(startTeachSession);
            Assert.NotNull(window.FindFirstDescendant(
                condition => condition.ByAutomationId("StopTeachSessionButton")));
            Assert.NotNull(window.FindFirstDescendant(
                condition => condition.ByAutomationId("TeachRecordingNameBox")));
            Assert.NotNull(window.FindFirstDescendant(
                condition => condition.ByAutomationId("TeachRecordingStatusText")));
            Assert.NotNull(window.FindFirstDescendant(
                condition => condition.ByAutomationId("TeachRecordingList")));
        }
        finally
        {
            CloseApplication(application);
        }

        var screenshotInfo = new ProcessStartInfo(dotnetHost)
        {
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        screenshotInfo.ArgumentList.Add(applicationAssembly);
        screenshotInfo.Environment["PANTHERA_UI_TEST"] = "1";
        screenshotInfo.Environment["PANTHERA_UI_ACCEPTANCE"] = "1";
        screenshotInfo.Environment["PANTHERA_SCREENSHOT_THEME"] = theme;
        screenshotInfo.Environment["PANTHERA_SCREENSHOT_PATH"] = screenshotPath;
        screenshotInfo.Environment["PANTHERA_FAILURE_LOG"] = Path.Combine(
            artifactDirectory,
            $"terminal-failures-screenshot-{theme.ToLowerInvariant()}.log");
        using var screenshotProcess = Process.Start(screenshotInfo)
            ?? throw new InvalidOperationException("WPF screenshot process did not start");
        Assert.True(screenshotProcess.WaitForExit(30_000), "WPF screenshot process did not exit");
        Assert.Equal(0, screenshotProcess.ExitCode);
        Assert.True(File.Exists(screenshotPath), $"Screenshot was not written to {screenshotPath}");
    }

    private static string PrepareArtifactDirectory(string testName)
    {
        var artifactDirectory = Environment.GetEnvironmentVariable("PANTHERA_UI_ARTIFACTS")
            ?? Path.Combine(AppContext.BaseDirectory, "ui-artifacts");
        Directory.CreateDirectory(artifactDirectory);
        File.WriteAllText(
            Path.Combine(artifactDirectory, $"started-{testName}.txt"),
            $"Started {DateTimeOffset.UtcNow:O}{Environment.NewLine}");
        return artifactDirectory;
    }

    private static (Application Application, UIA3Automation Automation, Window Window, string EventLog)
        LaunchAcceptanceApp(string testName)
    {
        var applicationAssembly = Path.Combine(AppContext.BaseDirectory, "Panthera.Terminal.App.dll");
        Assert.True(File.Exists(applicationAssembly), $"WPF app assembly was not copied to {applicationAssembly}");

        var dotnetRoot = Environment.GetEnvironmentVariable("DOTNET_ROOT")
            ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".dotnet");
        var dotnetHost = Path.Combine(dotnetRoot, "dotnet.exe");
        Assert.True(File.Exists(dotnetHost), $".NET host was not found at {dotnetHost}");

        var artifactDirectory = PrepareArtifactDirectory(testName);
        var eventLog = Path.Combine(artifactDirectory, $"events-{testName}.log");
        File.Delete(eventLog);
        var startInfo = new ProcessStartInfo(dotnetHost)
        {
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        startInfo.ArgumentList.Add(applicationAssembly);
        startInfo.Environment["PANTHERA_UI_TEST"] = "1";
        startInfo.Environment["PANTHERA_UI_ACCEPTANCE"] = "1";
        startInfo.Environment["PANTHERA_UI_ACCEPTANCE_LOG"] = eventLog;
        startInfo.Environment["PANTHERA_FAILURE_LOG"] = Path.Combine(
            artifactDirectory,
            $"terminal-failures-{testName}.log");

        var application = Application.Launch(startInfo);
        var automation = new UIA3Automation();
        var window = Retry.WhileNull(
            () => application.GetMainWindow(automation),
            TimeSpan.FromSeconds(15),
            TimeSpan.FromMilliseconds(200)).Result;
        Assert.NotNull(window);
        return (application, automation, window, eventLog);
    }

    private static void CloseApplication(Application application)
    {
        try
        {
            application.Close();
        }
        catch (InvalidOperationException)
        {
            return;
        }
        try
        {
            if (!application.HasExited)
            {
                application.Kill();
            }
        }
        catch (InvalidOperationException)
        {
        }
    }
}
