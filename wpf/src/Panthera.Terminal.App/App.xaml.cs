using System.Windows;
using System.Windows.Threading;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Panthera.Terminal.Core;
using Panthera.Terminal.Grpc;
using Panthera.Terminal.Settings;

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
        Current.ThemeMode = theme.ToLowerInvariant() switch
        {
            "light" => ThemeMode.Light,
            "dark" => ThemeMode.Dark,
            _ => ThemeMode.System,
        };
    }
}
