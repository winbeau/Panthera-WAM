using System.Text;
using Panthera.Terminal.Core;
using Panthera.Terminal.Settings;

namespace Panthera.Terminal.Tests;

public sealed class OpenSshRemoteDeploymentServiceTests
{
    [Fact]
    public void ParseProbeOutput_DecodesMachineReadableValues()
    {
        var output = string.Join('\n',
            Line("marker", "PANTHERA_SSH_PROBE_V1"),
            Line("arch", "aarch64"),
            Line("target_kind", "RaspberryPi/Linux ARM64"),
            Line("repo", "/home/winbeau/Panthera-WAM"),
            Line("start_method", "systemd-user"));

        var values = OpenSshRemoteDeploymentService.ParseProbeOutput(output);

        Assert.Equal("aarch64", values["arch"]);
        Assert.Equal("RaspberryPi/Linux ARM64", values["target_kind"]);
        Assert.Equal("/home/winbeau/Panthera-WAM", values["repo"]);
        Assert.Equal("systemd-user", values["start_method"]);
    }

    [Fact]
    public void BuildTunnelArguments_UsesLoopbackForBothGrpcServices()
    {
        var settings = new SshConnectionSettings(
            Host: "100.78.118.74",
            Port: 22,
            User: "winbeau",
            IdentityFile: Path.Combine(Path.GetTempPath(), "id_ed25519"));

        var args = OpenSshRemoteDeploymentService.BuildTunnelArguments(settings);

        Assert.Contains("127.0.0.1:50050:127.0.0.1:50051", args);
        Assert.Contains("127.0.0.1:50049:127.0.0.1:50052", args);
        Assert.Contains("ExitOnForwardFailure=yes", args);
        Assert.Contains("StrictHostKeyChecking=accept-new", args);
        Assert.Equal("winbeau@100.78.118.74", args[^1]);
    }

    [Fact]
    public void ProbeScript_DetectsDeploymentWithoutInstallingOrCloning()
    {
        var script = OpenSshRemoteDeploymentService.BuildProbeScript();

        Assert.Contains("uname -m", script, StringComparison.Ordinal);
        Assert.Contains("Panthera-WAM", script, StringComparison.Ordinal);
        Assert.Contains("systemctl --user cat armd.service", script, StringComparison.Ordinal);
        Assert.Contains("[ \"$target_kind\" = 'WSL' ]", script, StringComparison.Ordinal);
        Assert.Contains("import hightorque_robot", script, StringComparison.Ordinal);
        Assert.Contains("rs.__version__ == \"2.58.1\"", script, StringComparison.Ordinal);
        Assert.DoesNotContain("git clone", script, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("apt-get", script, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("uv sync", script, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void StartScript_UsesDetectedRepositoryAndExistingServices()
    {
        var script = OpenSshRemoteDeploymentService.BuildStartScript(
            "/home/user/Panthera-WAM",
            "systemd-user");

        Assert.Contains("cd '/home/user/Panthera-WAM'", script, StringComparison.Ordinal);
        Assert.Contains("systemctl --user start camerad.service armd.service", script, StringComparison.Ordinal);
        Assert.DoesNotContain("install", script, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("LISTEN 0 4096 127.0.0.1:50051\nLISTEN 0 4096 127.0.0.1:50052", true)]
    [InlineData("LISTEN 0 4096 127.0.0.1:50051", false)]
    [InlineData("", false)]
    public void HasListeningPorts_RequiresArmAndCamera(string output, bool expected)
    {
        Assert.Equal(expected, OpenSshRemoteDeploymentService.HasListeningPorts(output));
    }

    private static string Line(string key, string value) =>
        $"{key}\t{Convert.ToBase64String(Encoding.UTF8.GetBytes(value))}";
}
