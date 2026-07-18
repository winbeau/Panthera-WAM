using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

public partial class MainWindow : Window
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
    }

    private async void OnLoaded(object sender, RoutedEventArgs eventArgs)
    {
        _renderTimer.Start();
        await _viewModel.InitializeAsync();
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
}
