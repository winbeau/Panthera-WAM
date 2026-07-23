using System.Windows;
using System.Windows.Threading;
using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Panthera.Terminal.Core;
using Panthera.Terminal.Grpc;
using Panthera.Terminal.Settings;
using Wpf.Ui.Appearance;
using Wpf.Ui.Controls;

namespace Panthera.Terminal.App;

public partial class App : Application
{
    private IHost? _host;
    private Mutex? _singleInstanceMutex;
    private bool _ownsSingleInstanceMutex;

    protected override async void OnStartup(StartupEventArgs eventArgs)
    {
        base.OnStartup(eventArgs);
        await WaitForPriorInstanceAsync(eventArgs.Args);
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += OnUnhandledException;
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
        var uiTestMode = Environment.GetEnvironmentVariable("PANTHERA_UI_TEST") == "1";
        if (!uiTestMode)
        {
            _singleInstanceMutex = new Mutex(
                initiallyOwned: true,
                name: @"Local\Panthera.Terminal.App",
                createdNew: out _ownsSingleInstanceMutex);
            if (!_ownsSingleInstanceMutex)
            {
                if (!IsScreenshotMode)
                {
                    System.Windows.MessageBox.Show(
                        "Panthera-HT 控制终端已经在运行。请切换到现有窗口，避免两个终端争抢控制权。",
                        "终端已启动",
                        System.Windows.MessageBoxButton.OK,
                        System.Windows.MessageBoxImage.Information);
                }
                Shutdown();
                return;
            }
        }
        var settingsStore = new JsonTerminalSettingsStore();
        var settings = settingsStore.Load();
        var screenshotTheme = Environment.GetEnvironmentVariable("PANTHERA_SCREENSHOT_THEME");
        if (!string.IsNullOrWhiteSpace(screenshotTheme))
        {
            settings = settings with { Theme = screenshotTheme };
        }
        if (double.TryParse(Environment.GetEnvironmentVariable("PANTHERA_UI_SCALE_OVERRIDE"), out var uiScaleOverride))
        {
            settings = settings with { UiScale = Math.Clamp(uiScaleOverride, 0.90, 1.40) };
        }
        settings = ApplyConnectionOverrides(settings);
        ApplyTheme(settings.Theme);
        var uiAcceptanceMode = Environment.GetEnvironmentVariable("PANTHERA_UI_ACCEPTANCE") == "1";

        _host = Host.CreateDefaultBuilder()
            .ConfigureServices(services =>
            {
                services.AddSingleton<ITerminalSettingsStore>(settingsStore);
                services.AddSingleton(settings);
                services.AddSingleton<IArmdClient>(_ => uiAcceptanceMode
                    ? new UiAcceptanceArmdClient()
                    : new ArmdClient(
                        settings.Endpoint,
                        settings.CameraEndpoint,
                        settings.OverheadCameraEndpoint));
                services.AddSingleton<IEnvironmentGuideService, WindowsEnvironmentGuideService>();
                services.AddSingleton<IRemoteDeploymentService, OpenSshRemoteDeploymentService>();
                services.AddSingleton<ISshConnectionDiscoveryService, WindowsSshConnectionDiscoveryService>();
                services.AddSingleton<LatestStateSlot<RobotSnapshot>>();
                services.AddSingleton<LatestCameraFrames>();
                services.AddSingleton<MainWindowViewModel>();
                services.AddSingleton<MainWindow>();
                if (!uiAcceptanceMode && settings.UsesWslBridge)
                {
                    services.AddHostedService<WslTcpBridgeHostedService>();
                }
                else if (!uiAcceptanceMode && settings.UsesSshTunnel)
                {
                    services.AddHostedService<SshTunnelHostedService>();
                }
                services.AddHostedService<StateStreamHostedService>();
                services.AddHostedService<CameraStreamHostedService>();
            })
            .Build();
        await _host.StartAsync();
        _host.Services.GetRequiredService<MainWindow>().Show();
    }

    protected override async void OnExit(ExitEventArgs eventArgs)
    {
        if (_host is not null)
        {
            await _host.StopAsync(TimeSpan.FromSeconds(2));
            _host.Dispose();
        }
        if (_ownsSingleInstanceMutex)
        {
            try
            {
                _singleInstanceMutex?.ReleaseMutex();
            }
            catch (ApplicationException)
            {
            }
        }
        _singleInstanceMutex?.Dispose();
        _singleInstanceMutex = null;
        base.OnExit(eventArgs);
    }

    private static void OnDispatcherUnhandledException(
        object sender,
        DispatcherUnhandledExceptionEventArgs eventArgs)
    {
        AppDiagnostics.Write("dispatcher-unhandled", eventArgs.Exception);
        eventArgs.Handled = true;
    }

    private static void OnUnhandledException(object? sender, UnhandledExceptionEventArgs eventArgs)
    {
        if (eventArgs.ExceptionObject is Exception exception)
        {
            AppDiagnostics.Write("appdomain-unhandled", exception);
        }
    }

    private static void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs eventArgs)
    {
        AppDiagnostics.Write("task-unobserved", eventArgs.Exception);
        eventArgs.SetObserved();
    }

    public static void ApplyTheme(string theme)
    {
        CurrentTheme = theme;
        var applicationTheme = ResolveTheme(theme);
        var backdrop = IsScreenshotMode ? WindowBackdropType.None : WindowBackdropType.Mica;
        ApplicationThemeManager.Apply(applicationTheme, backdrop);
        foreach (var window in Current.Windows.OfType<MainWindow>())
        {
            window.ApplyThemePreference(theme);
        }
    }

    public static string CurrentTheme { get; private set; } = "System";

    public static bool IsScreenshotMode =>
        !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("PANTHERA_SCREENSHOT_PATH"))
        || Environment.GetEnvironmentVariable("PANTHERA_UI_TEST") == "1";

    private static TerminalSettings ApplyConnectionOverrides(TerminalSettings settings)
    {
        var backendMode = Environment.GetEnvironmentVariable("PANTHERA_BACKEND_MODE");
        var endpoint = Environment.GetEnvironmentVariable("PANTHERA_ENDPOINT");
        var cameraEndpoint = Environment.GetEnvironmentVariable("PANTHERA_CAMERA_ENDPOINT");
        var overheadCameraEndpoint = Environment.GetEnvironmentVariable(
            "PANTHERA_OVERHEAD_CAMERA_ENDPOINT");
        return settings with
        {
            BackendMode = string.IsNullOrWhiteSpace(backendMode) ? settings.BackendMode : backendMode,
            Endpoint = string.IsNullOrWhiteSpace(endpoint) ? settings.Endpoint : endpoint,
            CameraEndpoint = string.IsNullOrWhiteSpace(cameraEndpoint)
                ? settings.CameraEndpoint
                : cameraEndpoint,
            OverheadCameraEndpoint = string.IsNullOrWhiteSpace(overheadCameraEndpoint)
                ? settings.OverheadCameraEndpoint
                : overheadCameraEndpoint,
        };
    }

    private static ApplicationTheme ResolveTheme(string theme)
    {
        if (theme.Equals("light", StringComparison.OrdinalIgnoreCase))
        {
            return ApplicationTheme.Light;
        }
        if (theme.Equals("dark", StringComparison.OrdinalIgnoreCase))
        {
            return ApplicationTheme.Dark;
        }
        if (theme.Equals("highcontrast", StringComparison.OrdinalIgnoreCase))
        {
            return ApplicationTheme.HighContrast;
        }
        return ApplicationThemeManager.GetSystemTheme() switch
        {
            SystemTheme.Dark or SystemTheme.Glow or SystemTheme.CapturedMotion => ApplicationTheme.Dark,
            SystemTheme.HC1 or SystemTheme.HC2 or SystemTheme.HCBlack or SystemTheme.HCWhite => ApplicationTheme.HighContrast,
            _ => ApplicationTheme.Light,
        };
    }

    private static async Task WaitForPriorInstanceAsync(IReadOnlyList<string> arguments)
    {
        var marker = Array.IndexOf(arguments.ToArray(), "--wait-for-pid");
        if (marker < 0 || marker + 1 >= arguments.Count
            || !int.TryParse(arguments[marker + 1], out var processId)
            || processId == Environment.ProcessId)
        {
            return;
        }
        try
        {
            using var process = Process.GetProcessById(processId);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (ArgumentException)
        {
            // The old process has already exited.
        }
        catch (OperationCanceledException)
        {
            // Continue to the normal single-instance check, which remains the
            // final guard against overlapping control terminals.
        }
    }
}
