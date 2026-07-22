using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
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
    private readonly LatestCameraFrames _cameraFrames;
    private readonly IRemoteDeploymentService _remoteDeployment;
    private readonly ISshConnectionDiscoveryService _sshDiscovery;
    private readonly ITerminalSettingsStore _settingsStore;
    private readonly DispatcherTimer _renderTimer;
    private long _renderedVersion;
    private long _renderedColorVersion;
    private long _renderedDepthVersion;
    private bool _shutdownComplete;

    public MainWindow(
        MainWindowViewModel viewModel,
        LatestStateSlot<RobotSnapshot> stateSlot,
        LatestCameraFrames cameraFrames,
        IRemoteDeploymentService remoteDeployment,
        ISshConnectionDiscoveryService sshDiscovery,
        ITerminalSettingsStore settingsStore)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _stateSlot = stateSlot;
        _cameraFrames = cameraFrames;
        _remoteDeployment = remoteDeployment;
        _sshDiscovery = sshDiscovery;
        _settingsStore = settingsStore;
        DataContext = viewModel;
        if (App.IsScreenshotMode)
        {
            Width = ScreenshotDimension("PANTHERA_SCREENSHOT_WIDTH", 1240, MinWidth);
            Height = ScreenshotDimension("PANTHERA_SCREENSHOT_HEIGHT", 800, MinHeight);
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = 20;
            Top = 20;
        }
        else
        {
            ApplyAdaptiveWindowSize();
        }
        _renderTimer = new DispatcherTimer(DispatcherPriority.Render)
        {
            Interval = TimeSpan.FromMilliseconds(1000.0 / 30.0),
        };
        _renderTimer.Tick += RenderLatestState;
        Loaded += OnLoaded;
        Deactivated += OnDeactivated;
        Closing += OnClosing;
        AddHandler(
            Keyboard.PreviewKeyDownEvent,
            new KeyEventHandler(OnWindowPreviewKeyDown),
            handledEventsToo: true);
        ApplyThemePreference(viewModel.Theme);
    }

    private void OnWindowPreviewKeyDown(object sender, KeyEventArgs eventArgs)
    {
        if (eventArgs.Key != Key.F12 || eventArgs.IsRepeat)
        {
            return;
        }

        eventArgs.Handled = true;
        AppDiagnostics.Write("safety-input", "F12 preview key received");
        if (_viewModel.TriggerEStopCommand.CanExecute(null))
        {
            _viewModel.TriggerEStopCommand.Execute(null);
        }
    }

    private static double ScreenshotDimension(string variable, double fallback, double minimum) =>
        double.TryParse(Environment.GetEnvironmentVariable(variable), out var value)
            ? Math.Max(minimum, value)
            : fallback;

    private void ApplyAdaptiveWindowSize()
    {
        var workArea = SystemParameters.WorkArea;
        Width = Math.Clamp(Math.Floor(workArea.Width * 0.96), MinWidth, 1680);
        Height = Math.Clamp(Math.Floor(workArea.Height * 0.84), MinHeight, 920);
    }

    private async void OnLoaded(object sender, RoutedEventArgs eventArgs)
    {
        _renderTimer.Start();
        await _viewModel.InitializeAsync();
        if (App.IsScreenshotMode)
        {
            var cadTimeout = Environment.GetEnvironmentVariable("PANTHERA_VISUAL_QA_CAD") == "1"
                ? TimeSpan.FromSeconds(45)
                : TimeSpan.FromSeconds(15);
            await CadView.WaitUntilReadyAsync(cadTimeout);
        }
        await CaptureRequestedScreenshotAsync();
    }

    private void MainTab_Checked(object sender, RoutedEventArgs eventArgs)
    {
        if (ControlPage is null || DataPage is null || sender is not RadioButton { Tag: string page })
        {
            return;
        }
        var showControl = page.Equals("Control", StringComparison.OrdinalIgnoreCase);
        ControlPage.Visibility = showControl ? Visibility.Visible : Visibility.Collapsed;
        DataPage.Visibility = showControl ? Visibility.Collapsed : Visibility.Visible;
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
        _ = Dispatcher.BeginInvoke(new Action(() =>
        {
            if (IsLoaded)
            {
                Close();
            }
        }));
    }

    private void RenderLatestState(object? sender, EventArgs eventArgs)
    {
        var (snapshot, version) = _stateSlot.Read();
        if (snapshot is not null && version != _renderedVersion)
        {
            _renderedVersion = version;
            _viewModel.ApplySnapshot(snapshot);
        }
        RenderCameraFrame(CameraStreamKind.Color, ColorCameraImage, ref _renderedColorVersion);
        RenderCameraFrame(CameraStreamKind.Depth, DepthCameraImage, ref _renderedDepthVersion);
    }

    private void RenderCameraFrame(CameraStreamKind stream, Image image, ref long renderedVersion)
    {
        var (frame, version) = _cameraFrames.Read(stream);
        if (frame is null || version == renderedVersion)
        {
            return;
        }
        renderedVersion = version;
        var source = frame.PixelFormat switch
        {
            CameraPixelKind.Rgb8 => CreateColorBitmap(frame),
            CameraPixelKind.Z16 => CreateDepthBitmap(frame),
            _ => null,
        };
        image.Source = source;
        if (stream == CameraStreamKind.Color && source is not null)
        {
            CadView.UpdateColorCameraFrame(source);
        }
    }

    private static BitmapSource CreateColorBitmap(CameraFrameSnapshot frame)
    {
        var bitmap = new WriteableBitmap(
            frame.Width,
            frame.Height,
            96,
            96,
            PixelFormats.Rgb24,
            null);
        bitmap.WritePixels(
            new Int32Rect(0, 0, frame.Width, frame.Height),
            frame.Data,
            frame.Stride,
            0);
        bitmap.Freeze();
        return bitmap;
    }

    private static BitmapSource CreateDepthBitmap(CameraFrameSnapshot frame)
    {
        var pixels = new byte[frame.Width * frame.Height * 4];
        var scale = frame.DepthScale > 0 ? frame.DepthScale : 0.001;
        for (var y = 0; y < frame.Height; y++)
        {
            for (var x = 0; x < frame.Width; x++)
            {
                var source = y * frame.Stride + x * 2;
                if (source + 1 >= frame.Data.Length)
                {
                    continue;
                }
                var raw = frame.Data[source] | frame.Data[source + 1] << 8;
                var target = (y * frame.Width + x) * 4;
                if (raw == 0)
                {
                    pixels[target + 3] = 255;
                    continue;
                }
                var meters = raw * scale;
                var normalized = Math.Clamp((meters - 0.15) / 1.05, 0.0, 1.0);
                var near = 1.0 - normalized;
                pixels[target] = (byte)(255 * normalized);
                pixels[target + 1] = (byte)(255 * (1.0 - Math.Abs(near - 0.5) * 2.0));
                pixels[target + 2] = (byte)(255 * near);
                pixels[target + 3] = 255;
            }
        }
        var bitmap = BitmapSource.Create(
            frame.Width,
            frame.Height,
            96,
            96,
            PixelFormats.Bgra32,
            null,
            pixels,
            frame.Width * 4);
        bitmap.Freeze();
        return bitmap;
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
            "该操作会将机械臂与 D405 USB 统一挂载到 WSL，并可能触发 UAC 提权。流程只建立通道，不会下发运动指令。是否继续？",
            "确认环境引导",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning);
        if (result == MessageBoxResult.Yes && _viewModel.RunEnvironmentGuideCommand.CanExecute(null))
        {
            _viewModel.RunEnvironmentGuideCommand.Execute(null);
        }
    }

    private void OpenSshDeployment_Click(object sender, RoutedEventArgs eventArgs)
    {
        try
        {
            var dialog = new SshDeploymentDialog(_viewModel.Settings.SshSettings, _sshDiscovery)
            {
                Owner = this,
            };
            if (dialog.ShowDialog() != true || dialog.Result is not { } sshSettings)
            {
                return;
            }

            SshDeploymentButton.IsEnabled = false;
            var progressDialog = new SshDeploymentProgressDialog(sshSettings, _remoteDeployment)
            {
                Owner = this,
            };
            if (progressDialog.ShowDialog() != true
                || progressDialog.Report is not { Success: true })
            {
                return;
            }

            var updated = _viewModel.Settings with
            {
                BackendMode = "SshRemote",
                Endpoint = "http://127.0.0.1:50050",
                CameraEndpoint = "http://127.0.0.1:50049",
                Ssh = sshSettings,
            };
            _settingsStore.Save(updated);
            RestartApplication();
        }
        catch (Exception exception)
        {
            AppDiagnostics.Write("ssh-deployment", exception);
            MessageBox.Show(
                this,
                exception.Message,
                "SSH 部署失败",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
        }
        finally
        {
            SshDeploymentButton.IsEnabled = true;
        }
    }

    private void RestartApplication()
    {
        var executable = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(executable))
        {
            MessageBox.Show(this, "无法确定当前程序路径，请手动重启终端。", "需要重启");
            return;
        }
        var startInfo = new ProcessStartInfo
        {
            FileName = executable,
            UseShellExecute = true,
        };
        startInfo.ArgumentList.Add("--wait-for-pid");
        startInfo.ArgumentList.Add(Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture));
        Process.Start(startInfo);
        Close();
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
        var applicationTheme = theme.ToLowerInvariant() switch
        {
            "dark" => ApplicationTheme.Dark,
            "highcontrast" => ApplicationTheme.HighContrast,
            _ => ApplicationTheme.Light,
        };
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
