using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using Microsoft.Win32;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.App;

public partial class SshDeploymentDialog : Window
{
    private readonly SshConnectionSettings _previous;
    private readonly ISshConnectionDiscoveryService _discoveryService;
    private readonly CancellationTokenSource _discoveryCancellation = new();

    public SshDeploymentDialog(
        SshConnectionSettings current,
        ISshConnectionDiscoveryService discoveryService)
    {
        InitializeComponent();
        _previous = current;
        _discoveryService = discoveryService;
        HostBox.Text = current.Host;
        PortBox.Text = current.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        UserBox.Text = current.User;
        IdentityFileBox.Text = current.IdentityFile;
        AcceptHostKeyBox.IsChecked = current.AcceptNewHostKey;
        Loaded += SshDeploymentDialog_Loaded;
        Closed += (_, _) => _discoveryCancellation.Cancel();
    }

    public SshConnectionSettings? Result { get; private set; }

    private async void SshDeploymentDialog_Loaded(object sender, RoutedEventArgs eventArgs)
    {
        RecordAcceptanceEvent("ssh-dialog-opened");
        await RefreshCandidatesAsync();
    }

    private async void RefreshCandidates_Click(object sender, RoutedEventArgs eventArgs) =>
        await RefreshCandidatesAsync();

    private async Task RefreshCandidatesAsync()
    {
        var host = HostBox.Text;
        var user = UserBox.Text;
        DiscoveryText.Text = "正在探测…";
        RefreshCandidatesButton.IsEnabled = false;
        try
        {
            var candidates = await _discoveryService.DiscoverAsync(_previous, _discoveryCancellation.Token);
            HostBox.ItemsSource = candidates;
            UserBox.ItemsSource = candidates.Select(candidate => candidate.User)
                .Append(_previous.User)
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();

            HostBox.Text = host;
            UserBox.Text = user;
            if (string.IsNullOrWhiteSpace(host) && candidates.FirstOrDefault() is { } first)
            {
                ApplyCandidate(first);
            }
            RecordAcceptanceEvent("ssh-candidates-bound");
            DiscoveryText.Text = candidates.Count == 0 ? "未发现" : $"发现 {candidates.Count} 个";
        }
        catch (OperationCanceledException)
        {
        }
        catch
        {
            DiscoveryText.Text = "探测失败";
        }
        finally
        {
            RefreshCandidatesButton.IsEnabled = true;
        }
    }

    private void HostBox_SelectionChanged(object sender, SelectionChangedEventArgs eventArgs)
    {
        if (HostBox.SelectedItem is SshConnectionCandidate candidate)
        {
            ApplyCandidate(candidate);
        }
    }

    private void ApplyCandidate(SshConnectionCandidate candidate)
    {
        HostBox.Text = candidate.Host;
        PortBox.Text = candidate.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        if (!string.IsNullOrWhiteSpace(candidate.User))
        {
            UserBox.Text = candidate.User;
        }
        if (!string.IsNullOrWhiteSpace(candidate.IdentityFile))
        {
            IdentityFileBox.Text = candidate.IdentityFile;
        }
    }

    private static void RecordAcceptanceEvent(string eventName)
    {
        var path = Environment.GetEnvironmentVariable("PANTHERA_UI_ACCEPTANCE_LOG");
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }
        try
        {
            File.AppendAllText(path, $"{eventName}{Environment.NewLine}");
        }
        catch
        {
            // Test-only observability must never interfere with the dialog.
        }
    }

    private void Header_MouseLeftButtonDown(object sender, MouseButtonEventArgs eventArgs)
    {
        if (eventArgs.LeftButton == MouseButtonState.Pressed)
        {
            DragMove();
        }
    }

    private void BrowseIdentity_Click(object sender, RoutedEventArgs eventArgs)
    {
        var dialog = new OpenFileDialog
        {
            Title = "选择 OpenSSH 私钥",
            CheckFileExists = true,
            Multiselect = false,
            Filter = "OpenSSH 私钥|id_*;*.pem;*.key|所有文件|*.*",
        };
        if (dialog.ShowDialog(this) == true)
        {
            IdentityFileBox.Text = dialog.FileName;
        }
    }

    private void Submit_Click(object sender, RoutedEventArgs eventArgs)
    {
        ValidationText.Text = string.Empty;
        var host = HostBox.Text.Trim();
        var user = UserBox.Text.Trim();
        var identityFile = IdentityFileBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(host) || host.Contains("://", StringComparison.Ordinal))
        {
            ValidationText.Text = "请输入 SSH 主机名或 IP，不要包含 ssh:// 或 http://。";
            return;
        }
        if (!int.TryParse(PortBox.Text.Trim(), out var port) || port is <= 0 or > 65535)
        {
            ValidationText.Text = "SSH 端口必须是 1–65535 的整数。";
            return;
        }
        if (string.IsNullOrWhiteSpace(user))
        {
            ValidationText.Text = "请输入 SSH 用户名。";
            return;
        }
        if (!string.IsNullOrWhiteSpace(identityFile) && !File.Exists(identityFile))
        {
            ValidationText.Text = "指定的私钥文件不存在。";
            return;
        }

        Result = new SshConnectionSettings(
            host,
            port,
            user,
            identityFile,
            AcceptHostKeyBox.IsChecked == true);
        DialogResult = true;
    }
}
