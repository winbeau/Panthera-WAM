using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using Panthera.Terminal.Core;

namespace Panthera.Terminal.Settings;

/// <summary>
/// Discovers SSH connection candidates from local configuration and lightweight,
/// read-only host metadata. It never attempts authentication or scans address ranges.
/// </summary>
public sealed class WindowsSshConnectionDiscoveryService : ISshConnectionDiscoveryService
{
    private static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(4);

    public async Task<IReadOnlyList<SshConnectionCandidate>> DiscoverAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken = default)
    {
        var candidates = new List<SshConnectionCandidate>();
        if (!string.IsNullOrWhiteSpace(previous.Host))
        {
            candidates.Add(ToCandidate(previous, "上次使用"));
        }

        if (Environment.GetEnvironmentVariable("PANTHERA_UI_ACCEPTANCE") == "1")
        {
            return MergeCandidates(candidates);
        }

        var discoveryTasks = new Task<IReadOnlyList<SshConnectionCandidate>>[]
        {
            DiscoverOpenSshConfigAsync(previous, cancellationToken),
            DiscoverWslAsync(previous, cancellationToken),
            DiscoverTailscaleAsync(previous, cancellationToken),
            DiscoverMdnsAsync(previous, cancellationToken),
        };
        var discovered = await Task.WhenAll(discoveryTasks);
        foreach (var result in discovered)
        {
            candidates.AddRange(result);
        }
        return MergeCandidates(candidates);
    }

    internal static IReadOnlyList<string> ParseOpenSshAliases(string contents)
    {
        var aliases = new List<string>();
        foreach (var rawLine in contents.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith('#'))
            {
                continue;
            }
            var comment = line.IndexOf('#');
            if (comment >= 0)
            {
                line = line[..comment].TrimEnd();
            }
            var separator = line.IndexOfAny([' ', '\t']);
            if (separator <= 0 || !line[..separator].Equals("Host", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            foreach (var alias in line[(separator + 1)..].Split(
                [' ', '\t'],
                StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            {
                if (!alias.StartsWith('!') && alias.IndexOfAny(['*', '?']) < 0)
                {
                    aliases.Add(alias);
                }
            }
        }
        return aliases.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
    }

    internal static SshConnectionCandidate? ParseEffectiveSshConfig(string alias, string output)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var rawLine in output.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = rawLine.IndexOf(' ');
            if (separator <= 0)
            {
                continue;
            }
            var key = rawLine[..separator];
            var value = rawLine[(separator + 1)..].Trim();
            if (!values.ContainsKey(key))
            {
                values[key] = value;
            }
        }
        if (!values.TryGetValue("hostname", out var resolvedHost) || string.IsNullOrWhiteSpace(resolvedHost))
        {
            return null;
        }
        var port = values.TryGetValue("port", out var portText) && int.TryParse(portText, out var parsedPort)
            ? parsedPort
            : 22;
        values.TryGetValue("user", out var user);
        values.TryGetValue("identityfile", out var identityFile);
        identityFile = ExistingIdentityFile(identityFile ?? string.Empty);
        var source = resolvedHost.Equals(alias, StringComparison.OrdinalIgnoreCase)
            ? $"SSH config · {alias}"
            : $"SSH config · {alias} → {resolvedHost}";
        return new SshConnectionCandidate(alias, port, user ?? string.Empty, identityFile, source);
    }

    internal static (string User, IReadOnlyList<string> Addresses) ParseWslProbeOutput(string output)
    {
        var user = string.Empty;
        var addresses = Array.Empty<string>();
        foreach (var rawLine in output.Replace("\0", string.Empty, StringComparison.Ordinal)
                     .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var line = rawLine.Trim();
            if (line.StartsWith("user=", StringComparison.Ordinal))
            {
                user = line[5..].Trim();
            }
            else if (line.StartsWith("ips=", StringComparison.Ordinal))
            {
                addresses = line[4..].Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                    .Where(IsUsableIpv4)
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToArray();
            }
        }
        return (user, addresses);
    }

    internal static IReadOnlyList<SshConnectionCandidate> ParseTailscaleStatus(
        string json,
        SshConnectionSettings previous)
    {
        var candidates = new List<SshConnectionCandidate>();
        using var document = JsonDocument.Parse(json);
        if (!document.RootElement.TryGetProperty("Peer", out var peers)
            || peers.ValueKind != JsonValueKind.Object)
        {
            return candidates;
        }
        foreach (var peer in peers.EnumerateObject())
        {
            var value = peer.Value;
            var online = !value.TryGetProperty("Online", out var onlineElement) || onlineElement.GetBoolean();
            var os = StringProperty(value, "OS");
            var hostName = StringProperty(value, "HostName");
            var dnsName = StringProperty(value, "DNSName").TrimEnd('.');
            var displayName = !string.IsNullOrWhiteSpace(hostName) ? hostName : dnsName;
            if (!online || !os.Equals("linux", StringComparison.OrdinalIgnoreCase) || !LooksLikeRaspberryPi(displayName))
            {
                continue;
            }
            if (!value.TryGetProperty("TailscaleIPs", out var addresses)
                || addresses.ValueKind != JsonValueKind.Array)
            {
                continue;
            }
            foreach (var address in addresses.EnumerateArray().Select(element => element.GetString() ?? string.Empty)
                         .Where(IsUsableIpv4))
            {
                candidates.Add(new SshConnectionCandidate(
                    address,
                    MatchingPort(previous, address, hostName, dnsName),
                    previous.User,
                    previous.IdentityFile,
                    $"Raspberry Pi · Tailscale · {displayName}"));
            }
        }
        return candidates;
    }

    internal static IReadOnlyList<SshConnectionCandidate> MergeCandidates(
        IEnumerable<SshConnectionCandidate> candidates)
    {
        var merged = new Dictionary<string, SshConnectionCandidate>(StringComparer.OrdinalIgnoreCase);
        foreach (var candidate in candidates.Where(candidate =>
                     !string.IsNullOrWhiteSpace(candidate.Host) && candidate.Port is > 0 and <= 65535))
        {
            var normalized = candidate with
            {
                Host = candidate.Host.Trim(),
                User = candidate.User.Trim(),
                IdentityFile = candidate.IdentityFile.Trim(),
            };
            var key = $"{normalized.Host}\n{normalized.Port}";
            if (!merged.TryGetValue(key, out var existing))
            {
                merged[key] = normalized;
                continue;
            }
            if (string.IsNullOrWhiteSpace(existing.User) && !string.IsNullOrWhiteSpace(normalized.User))
            {
                merged[key] = normalized;
            }
        }
        return merged.Values.ToArray();
    }

    private static async Task<IReadOnlyList<SshConnectionCandidate>> DiscoverOpenSshConfigAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken)
    {
        try
        {
            var aliases = new List<string>();
            var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ssh", "config");
            if (File.Exists(path))
            {
                aliases.AddRange(ParseOpenSshAliases(await File.ReadAllTextAsync(path, cancellationToken)));
            }
            if (!string.IsNullOrWhiteSpace(previous.Host))
            {
                aliases.Insert(0, previous.Host);
            }
            var tasks = aliases.Distinct(StringComparer.OrdinalIgnoreCase).Take(32).Select(async alias =>
            {
                var result = await RunProcessAsync("ssh.exe", ["-G", alias], CommandTimeout, cancellationToken);
                return result is { ExitCode: 0 } ? ParseEffectiveSshConfig(alias, result.Output) : null;
            });
            return (await Task.WhenAll(tasks)).OfType<SshConnectionCandidate>().ToArray();
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            return [];
        }
    }

    private static async Task<IReadOnlyList<SshConnectionCandidate>> DiscoverWslAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken)
    {
        try
        {
            var list = await RunProcessAsync("wsl.exe", ["--list", "--quiet"], CommandTimeout, cancellationToken);
            if (list is not { ExitCode: 0 })
            {
                return [];
            }
            var distributions = list.Output.Replace("\0", string.Empty, StringComparison.Ordinal)
                .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(value => value.TrimStart('*').Trim())
                .Where(value => value.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Take(12)
                .ToArray();
            var localForwardHosts = await DiscoverLocalForwardHostsAsync(cancellationToken);
            var tasks = distributions.Select(async distribution =>
            {
                const string script = "printf 'user=%s\\n' \"$(id -un 2>/dev/null || true)\"; printf 'ips=%s\\n' \"$(hostname -I 2>/dev/null || true)\"";
                var probe = await RunProcessAsync(
                    "wsl.exe",
                    ["-d", distribution, "--", "sh", "-lc", script],
                    CommandTimeout,
                    cancellationToken);
                if (probe is not { ExitCode: 0 })
                {
                    return Array.Empty<SshConnectionCandidate>();
                }
                var (probedUser, addresses) = ParseWslProbeOutput(probe.Output);
                var user = string.IsNullOrWhiteSpace(probedUser) ? previous.User : probedUser;
                var found = addresses.Select(address => new SshConnectionCandidate(
                    address,
                    22,
                    user,
                    previous.IdentityFile,
                    $"WSL · {distribution}")).ToList();
                foreach (var forwardedHost in localForwardHosts)
                {
                    found.Insert(0, new SshConnectionCandidate(
                        forwardedHost,
                        2222,
                        user,
                        previous.IdentityFile,
                        forwardedHost == "127.0.0.1"
                            ? $"WSL · {distribution} · 本机转发"
                            : $"WSL · {distribution} · Tailscale 转发"));
                }
                return found.ToArray();
            });
            return (await Task.WhenAll(tasks)).SelectMany(values => values).ToArray();
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            return [];
        }
    }

    private static async Task<IReadOnlyList<SshConnectionCandidate>> DiscoverTailscaleAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken)
    {
        foreach (var executable in new[] { "tailscale.exe", "tailscale" })
        {
            try
            {
                var result = await RunProcessAsync(executable, ["status", "--json"], CommandTimeout, cancellationToken);
                if (result is { ExitCode: 0 })
                {
                    return ParseTailscaleStatus(result.Output, previous);
                }
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
            }
        }
        return [];
    }

    private static async Task<IReadOnlyList<SshConnectionCandidate>> DiscoverMdnsAsync(
        SshConnectionSettings previous,
        CancellationToken cancellationToken)
    {
        var names = new[] { "raspberrypi.local", "pi5.local", "panthera.local" };
        var tasks = names.Select(async name =>
        {
            try
            {
                var addresses = await Dns.GetHostAddressesAsync(name, cancellationToken)
                    .WaitAsync(TimeSpan.FromSeconds(2), cancellationToken);
                return addresses.Where(address => address.AddressFamily == AddressFamily.InterNetwork)
                    .Select(address => new SshConnectionCandidate(
                        address.ToString(),
                        MatchingPort(previous, address.ToString(), name),
                        previous.User,
                        previous.IdentityFile,
                        $"Raspberry Pi · mDNS · {name}"))
                    .ToArray();
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
                return Array.Empty<SshConnectionCandidate>();
            }
        });
        return (await Task.WhenAll(tasks)).SelectMany(values => values).ToArray();
    }

    private static async Task<ProcessResult?> RunProcessAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = fileName,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }
        using var process = Process.Start(startInfo);
        if (process is null)
        {
            return null;
        }
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeoutSource.CancelAfter(timeout);
        try
        {
            var stdout = process.StandardOutput.ReadToEndAsync(timeoutSource.Token);
            var stderr = process.StandardError.ReadToEndAsync(timeoutSource.Token);
            await process.WaitForExitAsync(timeoutSource.Token);
            return new ProcessResult(process.ExitCode, await stdout, await stderr);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            return null;
        }
    }

    private static async Task<bool> IsTcpPortOpenAsync(
        string host,
        int port,
        CancellationToken cancellationToken)
    {
        try
        {
            using var client = new TcpClient();
            await client.ConnectAsync(host, port, cancellationToken).AsTask()
                .WaitAsync(TimeSpan.FromMilliseconds(500), cancellationToken);
            return true;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            return false;
        }
    }

    private static async Task<IReadOnlyList<string>> DiscoverLocalForwardHostsAsync(
        CancellationToken cancellationToken)
    {
        var hosts = new List<string>();
        if (await IsTcpPortOpenAsync("127.0.0.1", 2222, cancellationToken))
        {
            hosts.Add("127.0.0.1");
        }
        foreach (var executable in new[] { "tailscale.exe", "tailscale" })
        {
            try
            {
                var result = await RunProcessAsync(executable, ["ip", "-4"], CommandTimeout, cancellationToken);
                if (result is not { ExitCode: 0 })
                {
                    continue;
                }
                foreach (var address in result.Output.Split(
                             ['\r', '\n'],
                             StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Where(IsUsableIpv4))
                {
                    if (await IsTcpPortOpenAsync(address, 2222, cancellationToken))
                    {
                        hosts.Add(address);
                    }
                }
                break;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
            }
        }
        return hosts.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
    }

    private static SshConnectionCandidate ToCandidate(SshConnectionSettings settings, string source) =>
        new(settings.Host, settings.Port, settings.User, settings.IdentityFile, source);

    private static string ExistingIdentityFile(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }
        var expanded = Environment.ExpandEnvironmentVariables(value);
        if (expanded.StartsWith("~/", StringComparison.Ordinal) || expanded.StartsWith("~\\", StringComparison.Ordinal))
        {
            expanded = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                expanded[2..]);
        }
        return File.Exists(expanded) ? Path.GetFullPath(expanded) : string.Empty;
    }

    private static string StringProperty(JsonElement element, string name) =>
        element.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.String
            ? property.GetString() ?? string.Empty
            : string.Empty;

    private static bool LooksLikeRaspberryPi(string value)
    {
        var normalized = value.Trim().TrimEnd('.').ToLowerInvariant();
        return normalized.Contains("raspberry", StringComparison.Ordinal)
            || normalized.Contains("panthera", StringComparison.Ordinal)
            || normalized.Equals("pi", StringComparison.Ordinal)
            || normalized.StartsWith("pi-", StringComparison.Ordinal)
            || normalized.StartsWith("pi_", StringComparison.Ordinal)
            || (normalized.StartsWith("pi", StringComparison.Ordinal)
                && normalized.Length > 2
                && char.IsDigit(normalized[2]));
    }

    private static int MatchingPort(SshConnectionSettings previous, params string[] hosts) =>
        hosts.Any(host => host.Equals(previous.Host, StringComparison.OrdinalIgnoreCase)) ? previous.Port : 22;

    private static bool IsUsableIpv4(string value) =>
        IPAddress.TryParse(value, out var address)
        && address.AddressFamily == AddressFamily.InterNetwork
        && !IPAddress.IsLoopback(address)
        && !address.Equals(IPAddress.Any)
        && !address.Equals(IPAddress.Broadcast);

    private sealed record ProcessResult(int ExitCode, string Output, string Error);
}
