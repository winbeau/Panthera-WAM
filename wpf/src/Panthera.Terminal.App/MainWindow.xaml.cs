using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Panthera.Terminal.Core;
using Wpf.Ui.Appearance;
using FluentWindow = Wpf.Ui.Controls.FluentWindow;
using WindowBackdropType = Wpf.Ui.Controls.WindowBackdropType;

namespace Panthera.Terminal.App;

public partial class MainWindow : FluentWindow
{
    private readonly MainWindowViewModel _viewModel;
    private readonly LatestStateSlot<RobotSnapshot> _stateSlot;
    private readonly DispatcherTimer _renderTimer;
    private long _renderedVersion;
    private bool _shutdownComplete;

    public MainWindow(MainWindowViewModel viewModel, LatestStateSlot<RobotSnapshot> stateSlot)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _stateSlot = stateSlot;
        DataContext = viewModel;
        _renderTimer = new DispatcherTimer(DispatcherPriority.Render)
        {
            Interval = TimeSpan.FromMilliseconds(1000.0 / 30.0),
        };
        _renderTimer.Tick += RenderLatestState;
        Loaded += OnLoaded;
        Deactivated += OnDeactivated;
        Closing += OnClosing;
        ApplyThemePreference(viewModel.Theme);
    }

    private async void OnLoaded(object sender, RoutedEventArgs eventArgs)
    {
        _renderTimer.Start();
        await _viewModel.InitializeAsync();
        await CaptureRequestedScreenshotAsync();
    }

    private async void OnDeactivated(object? sender, EventArgs eventArgs)
    {
        try
        {
            await _viewModel.StopJogAsync();
        }
        catch (Exception exception)
        {
            AppDiagnostics.Write("window-deactivated", exception);
        }
    }

    private async void OnClosing(object? sender, CancelEventArgs eventArgs)
    {
        if (_shutdownComplete)
        {
            return;
        }
        eventArgs.Cancel = true;
        _renderTimer.Stop();
        try
        {
            await _viewModel.ShutdownAsync();
        }
        catch (Exception exception)
        {
            AppDiagnostics.Write("window-closing", exception);
        }
        _shutdownComplete = true;
        Close();
    }

    private void RenderLatestState(object? sender, EventArgs eventArgs)
    {
        var (snapshot, version) = _stateSlot.Read();
        if (snapshot is null || version == _renderedVersion)
        {
            return;
        }
        _renderedVersion = version;
        _viewModel.ApplySnapshot(snapshot);
    }

    private void Theme_SelectionChanged(object sender, SelectionChangedEventArgs eventArgs)
    {
        if (!IsLoaded || sender is not ComboBox { SelectedItem: ComboBoxItem item })
        {
            return;
        }
        var theme = item.Content?.ToString() ?? "System";
        App.ApplyTheme(theme);
        _viewModel.SaveTheme(theme);
        InvalidateVisual();
    }

    private void RunEnvironment_Click(object sender, RoutedEventArgs eventArgs)
    {
        var result = MessageBox.Show(
            this,
            "该操作会将机械臂 USB 挂载到 WSL，并可能触发一次 UAC 提权。流程只建立通道，不会下发运动指令。是否继续？",
            "确认环境引导",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning);
        if (result == MessageBoxResult.Yes && _viewModel.RunEnvironmentGuideCommand.CanExecute(null))
        {
            _viewModel.RunEnvironmentGuideCommand.Execute(null);
        }
    }

    internal void ApplyThemePreference(string theme)
    {
        if (IsLoaded)
        {
            SystemThemeWatcher.UnWatch(this);
        }
        var backdrop = App.IsScreenshotMode ? WindowBackdropType.None : WindowBackdropType.Mica;
        WindowBackdropType = backdrop;
        if (theme.Equals("system", StringComparison.OrdinalIgnoreCase))
        {
            SystemThemeWatcher.Watch(this, backdrop, updateAccents: true);
            return;
        }
        var applicationTheme = theme.Equals("dark", StringComparison.OrdinalIgnoreCase)
            ? ApplicationTheme.Dark
            : ApplicationTheme.Light;
        ApplicationThemeManager.Apply(applicationTheme, backdrop, updateAccent: true);
    }

    private async Task CaptureRequestedScreenshotAsync()
    {
        var path = Environment.GetEnvironmentVariable("PANTHERA_SCREENSHOT_PATH");
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }

        await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        UpdateLayout();
        var dpi = VisualTreeHelper.GetDpi(this);
        var width = Math.Max(1, (int)Math.Ceiling(ActualWidth * dpi.DpiScaleX));
        var height = Math.Max(1, (int)Math.Ceiling(ActualHeight * dpi.DpiScaleY));
        var bitmap = new RenderTargetBitmap(width, height, 96 * dpi.DpiScaleX, 96 * dpi.DpiScaleY, PixelFormats.Pbgra32);
        bitmap.Render(this);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        Directory.CreateDirectory(Path.GetDirectoryName(path) ?? ".");
        await using (var stream = File.Create(path))
        {
            encoder.Save(stream);
        }
        await _viewModel.ShutdownAsync();
        _shutdownComplete = true;
        Application.Current.Shutdown();
    }
}
