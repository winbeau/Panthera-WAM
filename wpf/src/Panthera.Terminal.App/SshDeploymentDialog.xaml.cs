using System.IO;
using System.Windows;
using Microsoft.Win32;
using Panthera.Terminal.Core;
using Wpf.Ui.Controls;

namespace Panthera.Terminal.App;

public partial class SshDeploymentDialog : FluentWindow
{
    public SshDeploymentDialog(SshConnectionSettings current)
    {
        InitializeComponent();
        HostBox.Text = current.Host;
        PortBox.Text = current.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        UserBox.Text = current.User;
        IdentityFileBox.Text = current.IdentityFile;
        AcceptHostKeyBox.IsChecked = current.AcceptNewHostKey;
    }

    public SshConnectionSettings? Result { get; private set; }

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
