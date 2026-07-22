param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$page = "http://127.0.0.1:$Port/docs/mockups/panthera-six-axis-three-view.html"
$python = Get-Command python -ErrorAction Stop

Write-Host "Panthera-HT 六轴三视图：$page"
Write-Host "按 Ctrl+C 停止本地预览服务。"
Start-Process $page
& $python.Source -m http.server $Port --bind 127.0.0.1 --directory $repoRoot
