[CmdletBinding()]
param(
    [string]$Distribution = "Ubuntu-22.04",

    [ValidateRange(1, 30)]
    [int]$PollSeconds = 2,

    [ValidateRange(1, 600)]
    [int]$WaitSeconds = 30,

    [switch]$Watch,

    [switch]$InstallWatcher,

    [switch]$UninstallWatcher
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$taskName = "Panthera WAM USB Auto Attach"
$stateRoot = Join-Path $env:LOCALAPPDATA "Panthera-WAM"
$logPath = Join-Path $stateRoot "usb-auto-attach.log"
$usbipd = (Get-Command usbipd.exe -CommandType Application -ErrorAction Stop).Source
$wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop).Source
$targets = @(
    [pscustomobject]@{
        Name = "Panthera-HT"
        HardwareId = "caf1:ffff"
        InstancePattern = "VID_CAF1&PID_FFFF"
        Probe = "test `$(find /dev -maxdepth 1 -name 'ttyACM*' | wc -l) -ge 7"
    },
    [pscustomobject]@{
        Name = "RealSense D405"
        HardwareId = "8086:0b5b"
        InstancePattern = "VID_8086&PID_0B5B"
        Probe = "lsusb -d 8086:0b5b >/dev/null 2>&1"
    }
)
$lastObservedState = @{}

function Write-Log([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    if ($Watch) {
        New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
            Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
        }
        Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
    }
}

function Write-State([string]$Key, [string]$Value) {
    if (-not $lastObservedState.ContainsKey($Key) -or $lastObservedState[$Key] -ne $Value) {
        $lastObservedState[$Key] = $Value
        Write-Log "${Key}: $Value"
    }
}

function Invoke-External([string]$FilePath, [string[]]$Arguments) {
    $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        Success = $exitCode -eq 0
        ExitCode = $exitCode
        Output = ($output -join [Environment]::NewLine).Trim()
    }
}

function Get-UsbState {
    $result = Invoke-External $usbipd @("state")
    if (-not $result.Success) {
        throw "usbipd state failed ($($result.ExitCode)): $($result.Output)"
    }
    return $result.Output | ConvertFrom-Json
}

function Find-ConnectedDevice($State, $Target) {
    $matches = @($State.Devices | Where-Object {
        -not [string]::IsNullOrWhiteSpace([string]$_.BusId) -and
        ([string]$_.InstanceId).Contains($Target.InstancePattern, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($matches.Count -gt 1) {
        $ids = ($matches | ForEach-Object { "$($_.BusId) $($_.InstanceId)" }) -join "; "
        throw "Multiple $($Target.Name) devices matched: $ids"
    }
    return $matches | Select-Object -First 1
}

function Ensure-WslRunning {
    $result = Invoke-External $wsl @("-d", $Distribution, "--", "true")
    if (-not $result.Success) {
        throw "Unable to start WSL distribution '$Distribution': $($result.Output)"
    }
}

function Ensure-TargetAttached($Target) {
    $state = Get-UsbState
    $device = Find-ConnectedDevice $state $Target
    if ($null -eq $device) {
        Write-State $Target.Name "not connected to Windows"
        return $false
    }

    $busId = [string]$device.BusId
    if ([string]::IsNullOrWhiteSpace([string]$device.PersistedGuid)) {
        Write-Log "$($Target.Name): binding current BUSID $busId"
        $bind = Invoke-External $usbipd @("bind", "--busid", $busId)
        if (-not $bind.Success) {
            throw "Failed to bind $($Target.Name) at $busId. Run this script once in an elevated PowerShell. $($bind.Output)"
        }
        $state = Get-UsbState
        $device = Find-ConnectedDevice $state $Target
    }

    if ([string]::IsNullOrWhiteSpace([string]$device.ClientIPAddress)) {
        Ensure-WslRunning
        Write-Log "$($Target.Name): attaching dynamic BUSID $busId to $Distribution"
        $attach = Invoke-External $usbipd @(
            "attach", "--wsl", $Distribution, "--busid", $busId
        )
        if (-not $attach.Success) {
            throw "Failed to attach $($Target.Name) at ${busId}: $($attach.Output)"
        }
    }

    Write-State $Target.Name "attached at BUSID $busId"
    return $true
}

function Test-WslTarget($Target) {
    $probe = Invoke-External $wsl @(
        "-d", $Distribution, "--", "bash", "-lc", $Target.Probe
    )
    if ($probe.Success) {
        Write-State "$($Target.Name) WSL" "ready"
        return $true
    }
    Write-State "$($Target.Name) WSL" "waiting for Linux device enumeration"
    return $false
}

function Connect-All([bool]$KeepWaiting) {
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        $allReady = $true
        foreach ($target in $targets) {
            try {
                $attached = Ensure-TargetAttached $target
                $ready = $attached -and (Test-WslTarget $target)
                $allReady = $allReady -and $ready
            }
            catch {
                Write-State $target.Name "error: $($_.Exception.Message)"
                $allReady = $false
            }
        }
        if ($allReady) {
            if (-not $KeepWaiting) {
                return $true
            }
            Start-Sleep -Seconds $PollSeconds
            continue
        }
        if (-not $KeepWaiting -and (Get-Date) -ge $deadline) {
            return $false
        }
        Start-Sleep -Seconds $PollSeconds
    } while ($KeepWaiting -or (Get-Date) -lt $deadline)

    return $false
}

function Stop-LegacyAutoAttach {
    $targetBusIds = @()
    try {
        $state = Get-UsbState
        $targetBusIds = @($targets | ForEach-Object {
            $device = Find-ConnectedDevice $state $_
            if ($null -ne $device) {
                [string]$device.BusId
            }
        })
    }
    catch {
        Write-Log "Unable to inspect current BUSIDs while cleaning legacy watchers: $($_.Exception.Message)"
    }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'usbipd.exe'" | Where-Object {
        $commandLine = [string]$_.CommandLine
        $matchesTarget = $commandLine -match '(?i)caf1:ffff|8086:0b5b'
        foreach ($busId in $targetBusIds) {
            if ($commandLine -match "(?i)--busid\s+$([regex]::Escape($busId))(\s|$)") {
                $matchesTarget = $true
            }
        }
        $commandLine -match '(?i)\battach\b' -and
            $commandLine -match '(?i)--auto-attach' -and
            $matchesTarget
    })
    foreach ($process in $processes) {
        Write-Log "Stopping legacy usbipd auto-attach process $($process.ProcessId)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Install-WatcherTask {
    $pwsh = (Get-Command pwsh.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1).Source
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle Hidden",
        "-ExecutionPolicy Bypass",
        "-File `"$PSCommandPath`"",
        "-Watch",
        "-Distribution `"$Distribution`"",
        "-PollSeconds $PollSeconds"
    ) -join " "
    $userId = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew

    Stop-LegacyAutoAttach
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Dynamically attaches Panthera-HT and RealSense D405 to WSL by VID/PID." `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Log "Installed and started scheduled task '$taskName'"
}

function Uninstall-WatcherTask {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Log "Removed scheduled task '$taskName'"
}

if (($InstallWatcher -and $UninstallWatcher) -or ($Watch -and ($InstallWatcher -or $UninstallWatcher))) {
    throw "Use only one of -Watch, -InstallWatcher, or -UninstallWatcher."
}

if ($InstallWatcher) {
    Install-WatcherTask
    exit 0
}
if ($UninstallWatcher) {
    Uninstall-WatcherTask
    exit 0
}
if ($Watch) {
    $mutex = [Threading.Mutex]::new($false, "Local\PantheraWamUsbWatcher")
    $ownsMutex = $false
    try {
        try {
            $ownsMutex = $mutex.WaitOne(0)
        }
        catch [Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        if (-not $ownsMutex) {
            Write-Log "Another USB watcher is already running; exiting."
            exit 0
        }
        Write-Log "USB watcher started for $Distribution"
        [void](Connect-All $true)
    }
    finally {
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
    exit 0
}

if (-not (Connect-All $false)) {
    throw "Timed out after $WaitSeconds seconds waiting for Panthera-HT and RealSense D405 in WSL."
}
Write-Log "Panthera-HT and RealSense D405 are ready in $Distribution."
