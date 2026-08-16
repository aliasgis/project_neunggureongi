$ErrorActionPreference = "SilentlyContinue"
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pidFile = Join-Path $projectRoot "server.pid"
$portFile = Join-Path $projectRoot "server.port"
$projectPythons = @(
    (Join-Path $projectRoot "runtime\python.exe"),
    (Join-Path $projectRoot "venv\Scripts\python.exe")
)
$targets = [System.Collections.Generic.HashSet[int]]::new()
$serverPort = 8000
if (Test-Path -LiteralPath $portFile) {
    $savedPort = 0
    if ([int]::TryParse((Get-Content -LiteralPath $portFile -Raw).Trim(), [ref]$savedPort)) {
        $serverPort = $savedPort
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $savedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) {
        [void]$targets.Add($savedPid)
    }
}

Get-NetTCPConnection -LocalPort $serverPort -State Listen | ForEach-Object {
    [void]$targets.Add([int]$_.OwningProcess)
}

# Get-NetTCPConnection은 오래되었거나 권한이 제한된 수신 대상을 누락할 수 있습니다.
netstat -ano | Select-String "^\s*TCP\s+\S+:$serverPort\s+.*LISTENING\s+(\d+)\s*$" | ForEach-Object {
    if ($_.Matches.Count -gt 0) {
        [void]$targets.Add([int]$_.Matches[0].Groups[1].Value)
    }
}

# 이 프로젝트에 속한 이전의 다시 로드 모드 프로세스를 정리합니다.
Get-CimInstance Win32_Process | Where-Object {
    ($projectPythons -contains $_.ExecutablePath) -or
    ($_.CommandLine -and $_.CommandLine.Contains($projectRoot) -and
     (
        $_.Name -like "python*" -or
        $_.Name -like "uvicorn*"
     ))
} | ForEach-Object {
    [void]$targets.Add([int]$_.ProcessId)
}

if ($targets.Count -eq 0) {
    Write-Host "[INFO] No Project Neunggureongi server is running on port $serverPort."
    Remove-Item -LiteralPath $pidFile -Force
    Remove-Item -LiteralPath $portFile -Force
    exit 0
}

$stopped = 0
$orderedTargets = @($targets | Sort-Object {
    $candidate = Get-Process -Id $_
    if ($candidate -and $candidate.ProcessName -like "python*") { 0 } else { 1 }
})
foreach ($processId in $orderedTargets) {
    $process = Get-Process -Id $processId
    if ($process) {
        Write-Host "[STOP] PID $processId ($($process.ProcessName))"
        Stop-Process -Id $processId -Force
        if (-not (Get-Process -Id $processId)) {
            $stopped++
        }
    } else {
        Write-Host "[WARN] Port owner PID $processId is not present in the process table."
        taskkill.exe /PID $processId /T /F 2>$null | Out-Null
    }
}

Remove-Item -LiteralPath $pidFile -Force
Remove-Item -LiteralPath $portFile -Force
Start-Sleep -Milliseconds 500
$remaining = Get-NetTCPConnection -LocalPort $serverPort -State Listen
if ($remaining) {
    $remainingPids = @($remaining | Select-Object -ExpandProperty OwningProcess -Unique)
    $liveOwners = @($remainingPids | Where-Object { Get-Process -Id $_ })
    if ($liveOwners.Count -eq 0) {
        Write-Host "[ERROR] Windows reports a stale port $serverPort listener with no live process."
        Write-Host "        Restart Windows once to clear the stale TCP listener."
    } else {
        Write-Host "[ERROR] Port $serverPort is still in use by PID: $($liveOwners -join ', ')"
        Write-Host "        Run stop_server.bat as Administrator."
    }
    exit 1
}

Write-Host "[OK] Server and launcher processes stopped."
