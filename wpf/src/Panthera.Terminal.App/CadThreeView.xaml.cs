using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Panthera.Terminal.App;

public partial class CadThreeView : UserControl
{
    private static readonly TimeSpan CameraPushInterval = TimeSpan.FromSeconds(1.0 / 30.0);

    public static readonly DependencyProperty J1Property = StateProperty(nameof(J1));
    public static readonly DependencyProperty J2Property = StateProperty(nameof(J2));
    public static readonly DependencyProperty J3Property = StateProperty(nameof(J3));
    public static readonly DependencyProperty J4Property = StateProperty(nameof(J4));
    public static readonly DependencyProperty J5Property = StateProperty(nameof(J5));
    public static readonly DependencyProperty J6Property = StateProperty(nameof(J6));
    public static readonly DependencyProperty GripperPercentProperty = StateProperty(nameof(GripperPercent));
    public static readonly DependencyProperty TcpXProperty = DependencyProperty.Register(nameof(TcpX), typeof(double), typeof(CadThreeView), new PropertyMetadata(0.0));
    public static readonly DependencyProperty TcpYProperty = DependencyProperty.Register(nameof(TcpY), typeof(double), typeof(CadThreeView), new PropertyMetadata(0.0));
    public static readonly DependencyProperty TcpZProperty = DependencyProperty.Register(nameof(TcpZ), typeof(double), typeof(CadThreeView), new PropertyMetadata(0.0));
    public static readonly DependencyProperty ThemeProperty = DependencyProperty.Register(
        nameof(Theme),
        typeof(string),
        typeof(CadThreeView),
        new PropertyMetadata("System", OnThemeChanged));

    private readonly DispatcherTimer _pushTimer;
    private readonly TaskCompletionSource<string> _modelReady = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private bool _bridgeReady;
    private bool _stateDirty = true;
    private bool _pushActive;
    private bool _cameraPushActive;
    private bool _initializing;
    private DateTimeOffset _lastCameraPush = DateTimeOffset.MinValue;
    private WebView2? _browser;

    public CadThreeView()
    {
        InitializeComponent();
        _pushTimer = new DispatcherTimer(DispatcherPriority.Render)
        {
            Interval = TimeSpan.FromMilliseconds(1000.0 / 30.0),
        };
        _pushTimer.Tick += PushTimer_Tick;
        Loaded += CadThreeView_Loaded;
        Unloaded += CadThreeView_Unloaded;
    }

    public double J1 { get => (double)GetValue(J1Property); set => SetValue(J1Property, value); }
    public double J2 { get => (double)GetValue(J2Property); set => SetValue(J2Property, value); }
    public double J3 { get => (double)GetValue(J3Property); set => SetValue(J3Property, value); }
    public double J4 { get => (double)GetValue(J4Property); set => SetValue(J4Property, value); }
    public double J5 { get => (double)GetValue(J5Property); set => SetValue(J5Property, value); }
    public double J6 { get => (double)GetValue(J6Property); set => SetValue(J6Property, value); }
    public double GripperPercent { get => (double)GetValue(GripperPercentProperty); set => SetValue(GripperPercentProperty, value); }
    public double TcpX { get => (double)GetValue(TcpXProperty); set => SetValue(TcpXProperty, value); }
    public double TcpY { get => (double)GetValue(TcpYProperty); set => SetValue(TcpYProperty, value); }
    public double TcpZ { get => (double)GetValue(TcpZProperty); set => SetValue(TcpZProperty, value); }
    public string Theme { get => (string)GetValue(ThemeProperty); set => SetValue(ThemeProperty, value); }

    public void UpdateColorCameraFrame(BitmapSource frame)
    {
        if (!_bridgeReady || _cameraPushActive || _browser?.CoreWebView2 is null
            || DateTimeOffset.UtcNow - _lastCameraPush < CameraPushInterval)
        {
            return;
        }
        _ = PushColorCameraFrameAsync(frame);
    }

    public async Task<bool> WaitUntilReadyAsync(TimeSpan timeout)
    {
        var completed = await Task.WhenAny(_modelReady.Task, Task.Delay(timeout));
        return completed == _modelReady.Task;
    }

    private static DependencyProperty StateProperty(string name) => DependencyProperty.Register(
        name,
        typeof(double),
        typeof(CadThreeView),
        new PropertyMetadata(0.0, OnStateChanged));

    private static void OnStateChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        ((CadThreeView)dependencyObject)._stateDirty = true;

    private static void OnThemeChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        _ = ((CadThreeView)dependencyObject).ApplyThemeAsync();

    private async void CadThreeView_Loaded(object sender, RoutedEventArgs eventArgs)
    {
        if (Environment.GetEnvironmentVariable("PANTHERA_UI_ACCEPTANCE") == "1"
            && Environment.GetEnvironmentVariable("PANTHERA_VISUAL_QA_CAD") != "1")
        {
            LoadingText.Text = "CAD UI 验收占位";
            _modelReady.TrySetResult("acceptance");
            return;
        }
        _pushTimer.Start();
        await InitializeBrowserAsync();
    }

    private void CadThreeView_Unloaded(object sender, RoutedEventArgs eventArgs) => _pushTimer.Stop();

    private async Task InitializeBrowserAsync()
    {
        if (_initializing || _browser?.CoreWebView2 is not null)
        {
            return;
        }
        _initializing = true;
        try
        {
            var browser = _browser;
            if (browser is null)
            {
                browser = new WebView2
                {
                    Focusable = false,
                    IsHitTestVisible = false,
                    HorizontalAlignment = HorizontalAlignment.Stretch,
                    VerticalAlignment = VerticalAlignment.Stretch,
                };
                _browser = browser;
                BrowserHost.Children.Insert(0, browser);
            }
            await browser.EnsureCoreWebView2Async();
            var core = browser.CoreWebView2
                ?? throw new InvalidOperationException("WebView2 初始化完成后 CoreWebView2 仍为空");
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreDevToolsEnabled = Debugger.IsAttached;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.SetVirtualHostNameToFolderMapping(
                "panthera.assets",
                AppContext.BaseDirectory,
                CoreWebView2HostResourceAccessKind.Allow);
            core.WebMessageReceived += Core_WebMessageReceived;
            core.Navigate("https://panthera.assets/TriView/panthera-wpf-host.html");
        }
        catch (Exception exception)
        {
            LoadingText.Text = $"CAD 初始化失败：{exception.Message}";
            _modelReady.TrySetResult("failed");
        }
        finally
        {
            _initializing = false;
        }
    }

    private void Core_WebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs eventArgs)
    {
        try
        {
            using var message = JsonDocument.Parse(eventArgs.WebMessageAsJson);
            var root = message.RootElement;
            var type = root.TryGetProperty("type", out var typeValue) ? typeValue.GetString() : string.Empty;
            if (type == "bridge-ready")
            {
                _bridgeReady = true;
                _stateDirty = true;
                _ = ApplyThemeAsync();
                return;
            }
            if (type == "model-ready")
            {
                var model = root.TryGetProperty("model", out var modelValue) ? modelValue.GetString() : "unknown";
                LoadingText.Text = model == "exact" ? "EXACT GLB" : "轻量 CAD";
                LoadingOverlay.Visibility = Visibility.Collapsed;
                _modelReady.TrySetResult(model ?? "unknown");
            }
        }
        catch (JsonException exception)
        {
            AppDiagnostics.Write("cad-web-message", exception);
        }
    }

    private async void PushTimer_Tick(object? sender, EventArgs eventArgs)
    {
        var browser = _browser;
        if (!_bridgeReady || !_stateDirty || _pushActive || browser?.CoreWebView2 is null)
        {
            return;
        }
        _stateDirty = false;
        _pushActive = true;
        try
        {
            var payload = JsonSerializer.Serialize(new
            {
                positions = new[] { J1, J2, J3, J4, J5, J6 },
                gripper = Math.Clamp(GripperPercent / 100.0, 0, 1),
                source = "WPF",
                timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            });
            await browser.ExecuteScriptAsync($"window.PantheraWpfBridge?.setState({payload})");
        }
        catch (Exception exception) when (exception is InvalidOperationException or ObjectDisposedException)
        {
            _stateDirty = true;
        }
        finally
        {
            _pushActive = false;
        }
    }

    private async Task ApplyThemeAsync()
    {
        var browser = _browser;
        if (!_bridgeReady || browser?.CoreWebView2 is null)
        {
            return;
        }
        var theme = Theme.Equals("Dark", StringComparison.OrdinalIgnoreCase)
            || Theme.Equals("HighContrast", StringComparison.OrdinalIgnoreCase)
            ? "dark"
            : "light";
        await browser.ExecuteScriptAsync(
            $"window.PantheraWpfBridge?.setTheme({JsonSerializer.Serialize(theme)})");
    }

    private async Task PushColorCameraFrameAsync(BitmapSource frame)
    {
        var browser = _browser;
        if (browser?.CoreWebView2 is null)
        {
            return;
        }
        _cameraPushActive = true;
        try
        {
            var dataUrl = await Task.Run(() => EncodeJpegDataUrl(frame));
            await browser.ExecuteScriptAsync(
                $"window.PantheraWpfBridge?.setCameraFrame({JsonSerializer.Serialize(dataUrl)})");
            _lastCameraPush = DateTimeOffset.UtcNow;
        }
        catch (Exception exception) when (exception is InvalidOperationException or ObjectDisposedException)
        {
            AppDiagnostics.Write("cad-camera-frame", exception);
        }
        finally
        {
            _cameraPushActive = false;
        }
    }

    private static string EncodeJpegDataUrl(BitmapSource frame)
    {
        using var stream = new MemoryStream();
        var encoder = new JpegBitmapEncoder { QualityLevel = 72 };
        encoder.Frames.Add(BitmapFrame.Create(frame));
        encoder.Save(stream);
        return $"data:image/jpeg;base64,{Convert.ToBase64String(stream.ToArray())}";
    }

    private async void StylePicker_SelectionChanged(object sender, SelectionChangedEventArgs eventArgs)
    {
        var browser = _browser;
        if (!_bridgeReady || browser?.CoreWebView2 is null || StylePicker.SelectedItem is not ComboBoxItem { Tag: string style })
        {
            return;
        }
        await browser.ExecuteScriptAsync(
            $"window.PantheraWpfBridge?.setStyle({JsonSerializer.Serialize(style)})");
    }

    private async void CameraPicker_SelectionChanged(object sender, SelectionChangedEventArgs eventArgs)
    {
        var browser = _browser;
        if (!_bridgeReady || browser?.CoreWebView2 is null || CameraPicker.SelectedItem is not ComboBoxItem { Tag: string mode })
        {
            return;
        }
        await browser.ExecuteScriptAsync(
            $"window.PantheraWpfBridge?.setCameraMode({JsonSerializer.Serialize(mode)})");
    }
}
