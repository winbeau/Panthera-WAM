using System.Windows;
using System.Windows.Threading;
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

    protected override async void OnStartup(StartupEventArgs eventArgs)
    {
        base.OnStartup(eventArgs);
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += OnUnhandledException;
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
        var settingsStore = new JsonTerminalSettingsStore();
        var settings = settingsStore.Load();
        var screenshotTheme = Environment.GetEnvironmentVariable("PANTHERA_SCREENSHOT_THEME");
        if (!string.IsNullOrWhiteSpace(screenshotTheme))
        {
            settings = settings with { Theme = screenshotTheme };
        }
        ApplyTheme(settings.Theme);

        _host = Host.CreateDefaultBuilder()
            .ConfigureServices(services =>
            {
                services.AddSingleton<ITerminalSettingsStore>(settingsStore);
                services.AddSingleton(settings);
                services.AddSingleton<IArmdClient>(_ => new ArmdClient(settings.Endpoint));
                services.AddSingleton<IEnvironmentGuideService, WindowsEnvironmentGuideService>();
                services.AddSingleton<LatestStateSlot<RobotSnapshot>>();
                services.AddSingleton<MainWindowViewModel>();
                services.AddSingleton<MainWindow>();
                services.AddHostedService<WslTcpBridgeHostedService>();
                services.AddHostedService<StateStreamHostedService>();
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
        base.OnExit(eventArgs);
    }

    private static void OnDispatcherUnhandledException(
        object sender,
        DispatcherUnhandledExceptionEventArgs eventArgs) =>
        AppDiagnostics.Write("dispatcher-unhandled", eventArgs.Exception);

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
}
