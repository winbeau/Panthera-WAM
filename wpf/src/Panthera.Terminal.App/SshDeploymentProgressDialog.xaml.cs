using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

public partial class SshDeploymentProgressDialog : Window
{
    private readonly SshConnectionSettings _settings;
    private readonly IRemoteDeploymentService _deploymentService;
    private readonly CancellationTokenSource _cancellation = new();
    private bool _isRunning = true;
    private bool _closeRequested;

    public SshDeploymentProgressDialog(
        SshConnectionSettings settings,
        IRemoteDeploymentService deploymentService)
    {
        InitializeComponent();
        _settings = settings;
        _deploymentService = deploymentService;
        Steps = new ObservableCollection<DeploymentStepItem>(
            RemoteDeploymentProgress.OrderedStepNames.Select(name => new DeploymentStepItem(name)));
        DataContext = this;
        TargetText.Text = $"{settings.User}@{settings.Host}:{settings.Port}";
        Loaded += SshDeploymentProgressDialog_Loaded;
        Closing += SshDeploymentProgressDialog_Closing;
        Closed += (_, _) => _cancellation.Dispose();
    }

    public ObservableCollection<DeploymentStepItem> Steps { get; }

    public RemoteDeploymentReport? Report { get; private set; }

    private async void SshDeploymentProgressDialog_Loaded(object sender, RoutedEventArgs eventArgs)
    {
        var progress = new DispatcherProgress<RemoteDeploymentProgress>(Dispatcher, UpdateProgress);
        try
        {
            Report = await _deploymentService.ConfigureAndStartAsync(
                _settings,
                progress,
                _cancellation.Token);
        }
        catch (Exception exception)
        {
            var step = Steps.FirstOrDefault(item => item.State == RemoteDeploymentProgressState.Running)
                ?? Steps.FirstOrDefault(item => item.State == RemoteDeploymentProgressState.Pending)
                ?? Steps[^1];
            step.Update(RemoteDeploymentProgressState.Failed, exception.Message);
            Report = new RemoteDeploymentReport(
                [new EnvironmentGuideStep(step.Name, false, exception.Message, "ssh")]);
        }
        finally
        {
            CompleteProgress();
        }
    }

    private void UpdateProgress(RemoteDeploymentProgress progress)
    {
        var step = Steps.FirstOrDefault(item => string.Equals(item.Name, progress.Name, StringComparison.Ordinal));
        if (step is null)
        {
            return;
        }
        step.Update(progress.State, progress.Detail);
        var completed = Steps.Count(item => item.State is
            RemoteDeploymentProgressState.Succeeded or RemoteDeploymentProgressState.Failed);
        OverallProgress.Value = completed;
        ProgressCountText.Text = $"{completed} / {Steps.Count}";
        if (progress.State == RemoteDeploymentProgressState.Running)
        {
            HeadlineText.Text = progress.Name;
            SummaryText.Text = progress.Detail;
        }
    }

    private void CompleteProgress()
    {
        _isRunning = false;
        var success = Report?.Success == true;
        foreach (var step in Steps.Where(item => item.State == RemoteDeploymentProgressState.Pending))
        {
            step.MarkSkipped();
        }
        var completed = Steps.Count(item => item.State is
            RemoteDeploymentProgressState.Succeeded or RemoteDeploymentProgressState.Failed);
        OverallProgress.Value = completed;
        ProgressCountText.Text = $"{completed} / {Steps.Count}";
        HeadlineText.Text = success ? "部署链路已就绪" : "部署未完成";
        SummaryText.Text = success
            ? "连接配置将在确认后保存，终端会自动重启并建立 SSH 隧道。"
            : Report?.Steps.FirstOrDefault(step => !step.Success)?.Detail ?? "远程部署已取消。";
        ActionButton.Content = success ? "完成并重启" : "关闭";
        ActionButton.IsEnabled = true;
        if (_closeRequested)
        {
            DialogResult = false;
        }
    }

    private void CloseButton_Click(object sender, RoutedEventArgs eventArgs) => Close();

    private void ActionButton_Click(object sender, RoutedEventArgs eventArgs) =>
        DialogResult = Report?.Success == true;

    private void SshDeploymentProgressDialog_Closing(object? sender, CancelEventArgs eventArgs)
    {
        if (!_isRunning)
        {
            return;
        }
        eventArgs.Cancel = true;
        _closeRequested = true;
        CloseButton.IsEnabled = false;
        HeadlineText.Text = "正在取消…";
        SummaryText.Text = "正在结束当前 SSH 命令。";
        _cancellation.Cancel();
    }

    private void Header_MouseLeftButtonDown(object sender, MouseButtonEventArgs eventArgs)
    {
        if (eventArgs.LeftButton == MouseButtonState.Pressed)
        {
            DragMove();
        }
    }
}

internal sealed class DispatcherProgress<T>(Dispatcher dispatcher, Action<T> callback) : IProgress<T>
{
    public void Report(T value)
    {
        if (dispatcher.CheckAccess())
        {
            callback(value);
            return;
        }
        dispatcher.Invoke(() => callback(value));
    }
}

public sealed class DeploymentStepItem : INotifyPropertyChanged
{
    private RemoteDeploymentProgressState _state = RemoteDeploymentProgressState.Pending;
    private string _detail = "等待开始";

    public DeploymentStepItem(string name)
    {
        Name = name;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public string Name { get; }

    public RemoteDeploymentProgressState State
    {
        get => _state;
        private set
        {
            if (_state == value)
            {
                return;
            }
            _state = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(StatusText));
        }
    }

    public string Detail
    {
        get => _detail;
        private set
        {
            if (string.Equals(_detail, value, StringComparison.Ordinal))
            {
                return;
            }
            _detail = value;
            OnPropertyChanged();
        }
    }

    public string StatusText => State switch
    {
        RemoteDeploymentProgressState.Running => "进行中",
        RemoteDeploymentProgressState.Succeeded => "完成",
        RemoteDeploymentProgressState.Failed => "失败",
        _ => "等待",
    };

    public void Update(RemoteDeploymentProgressState state, string detail)
    {
        State = state;
        Detail = string.IsNullOrWhiteSpace(detail) ? StatusText : detail;
    }

    public void MarkSkipped() => Detail = "未执行";

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
