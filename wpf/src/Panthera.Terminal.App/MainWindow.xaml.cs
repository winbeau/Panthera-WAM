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
    private readonly LatestCameraFrames _cameraFrames;
    private readonly DispatcherTimer _renderTimer;
    private long _renderedVersion;
    private long _renderedColorVersion;
    private long _renderedDepthVersion;
    private bool _shutdownComplete;

    public MainWindow(
        MainWindowViewModel viewModel,
        LatestStateSlot<RobotSnapshot> stateSlot,
        LatestCameraFrames cameraFrames)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _stateSlot = stateSlot;
        _cameraFrames = cameraFrames;
        DataContext = viewModel;
        if (App.IsScreenshotMode)
        {
            Width = 1240;
            Height = 800;
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = 20;
            Top = 20;
        }
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
        image.Source = frame.PixelFormat switch
        {
            CameraPixelKind.Rgb8 => CreateColorBitmap(frame),
            CameraPixelKind.Z16 => CreateDepthBitmap(frame),
            _ => null,
        };
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
