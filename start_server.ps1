$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runtimePython = Join-Path $projectRoot "runtime\python.exe"
$venvPython = Join-Path $projectRoot "venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { $venvPython }
$configPath = Join-Path $projectRoot "config.json"
$pidFile = Join-Path $projectRoot "server.pid"
$portFile = Join-Path $projectRoot "server.port"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "[ERROR] Python virtual environment was not found."
    Write-Host "        Reinstall the application or run install_and_run.bat first."
    exit 1
}

# 다음 사용 가능한 포트에 두 번째 인스턴스를 시작하지 않습니다.
if ((Test-Path -LiteralPath $pidFile) -and (Test-Path -LiteralPath $portFile)) {
    $existingPid = 0
    $existingPort = 0
    $pidValid = [int]::TryParse(
        (Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$existingPid
    )
    $portValid = [int]::TryParse(
        (Get-Content -LiteralPath $portFile -Raw).Trim(), [ref]$existingPort
    )
    if ($pidValid -and $portValid) {
        $existingListener = Get-NetTCPConnection -LocalPort $existingPort `
            -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $existingPid }
        if ($existingListener) {
            Write-Host "[INFO] Project Neunggureongi is already running."
            Write-Host "[INFO] PID         : $existingPid"
            Write-Host "[INFO] Server URL  : http://127.0.0.1:$existingPort"
            Write-Host "[INFO] Admin Login: http://127.0.0.1:$existingPort/admin/login"
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
    Remove-Item -LiteralPath $portFile -Force
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding utf8 | ConvertFrom-Json
$serverConfig = $config.server
$hostAddress = if ($serverConfig.host) { [string]$serverConfig.host } else { "0.0.0.0" }
$configuredPort = if ($serverConfig.port) { [int]$serverConfig.port } else { 8000 }
$fallbackEnabled = if ($null -ne $serverConfig.fallback_on_conflict) {
    [bool]$serverConfig.fallback_on_conflict
} else {
    $true
}
$maximumPort = if ($serverConfig.fallback_max_port) {
    [int]$serverConfig.fallback_max_port
} else {
    $configuredPort + 10
}
$candidatePorts = if ($fallbackEnabled) {
    $configuredPort..([Math]::Max($configuredPort, $maximumPort))
} else {
    @($configuredPort)
}

$selectedPort = $null
foreach ($candidate in $candidatePorts) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Any, $candidate
        )
        $listener.Start()
        $selectedPort = $candidate
        break
    } catch {
        Write-Host "[INFO] Port $candidate is already in use."
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

if ($null -eq $selectedPort) {
    Write-Host "[ERROR] No available server port was found."
    Write-Host "        Check the server section in config.json."
    exit 1
}

$env:APP_PORT = [string]$selectedPort
Set-Content -LiteralPath $portFile `
    -Value $selectedPort -Encoding ascii -NoNewline

Write-Host ""
Write-Host "[START] Project Neunggureongi v1.0.0"
Write-Host "[START] Server URL  : http://127.0.0.1:$selectedPort"
Write-Host "[START] Admin Login: http://127.0.0.1:$selectedPort/admin/login"
Write-Host ""

Set-Location -LiteralPath $projectRoot
& $python -m uvicorn app:app --host $hostAddress --port $selectedPort
exit $LASTEXITCODE
