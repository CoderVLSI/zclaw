param(
    [int]$Port = 8790,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Split-Path -Parent $ProjectDir
$Python = Join-Path $RuntimeDir ".venv_runtime\Scripts\pythonw.exe"
$PythonConsole = Join-Path $RuntimeDir ".venv_runtime\Scripts\python.exe"
$Dashboard = Join-Path $PSScriptRoot "zclaw_dashboard.py"
$Requirements = Join-Path $PSScriptRoot "requirements-dashboard.txt"
$StateDir = Join-Path $ProjectDir ".local-dashboard"
$PidFile = Join-Path $StateDir "server.pid"
$LogFile = Join-Path $StateDir "server.log"
$ErrorLogFile = Join-Path $StateDir "server-error.log"
$Url = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Workspace Python runtime is missing: $Python"
}

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null

if ($Stop) {
    if (Test-Path -LiteralPath $PidFile) {
        $ExistingPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
        if ($ExistingPid -and (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue)) {
            Start-Process `
                -FilePath "$env:SystemRoot\System32\taskkill.exe" `
                -ArgumentList @("/PID", "$ExistingPid", "/T", "/F") `
                -WindowStyle Hidden `
                -Wait | Out-Null
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    Write-Output "zclaw dashboard stopped."
    exit 0
}

& $PythonConsole -c "import serial, esp_idf_nvs_partition_gen" 2>$null
if ($LASTEXITCODE -ne 0) {
    $Install = Start-Process `
        -FilePath $PythonConsole `
        -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-r", "`"$Requirements`"") `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($Install.ExitCode -ne 0) {
        throw "Dashboard Python dependencies could not be installed."
    }
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
    if ($ExistingPid -and (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue)) {
        if (-not $NoBrowser) {
            Start-Process $Url
        }
        Write-Output "zclaw dashboard is already running at $Url"
        exit 0
    }
}

$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList @("`"$Dashboard`"", "--port", "$Port") `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrorLogFile `
    -PassThru

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ascii
Start-Sleep -Milliseconds 900
if ($Process.HasExited) {
    throw "Dashboard failed to start. See $LogFile"
}

if (-not $NoBrowser) {
    Start-Process $Url
}
Write-Output "zclaw dashboard started at $Url (PID $($Process.Id))"
