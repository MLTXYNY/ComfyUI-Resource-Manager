param([string]$Action = "start")
$ErrorActionPreference = "SilentlyContinue"
$Port = 8000
$Url = "http://127.0.0.1:$Port"

function Test-Port {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if ($Action -eq "stop") {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($c) {
        $c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }
    if (Test-Port) {
        Write-Host "STOP FAILED: port $Port still in use."
    } else {
        Write-Host "Service stopped."
    }
    exit
}

# ---- start ----
if (Test-Port) {
    Write-Host "Service already running: $Url"
    Start-Process $Url
    exit
}

$py = "C:\Users\ML167\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "Starting service, please wait..."
Start-Process -FilePath $py -ArgumentList "app.py" -WorkingDirectory $dir -WindowStyle Hidden
Start-Sleep -Seconds 3

if (Test-Port) {
    Write-Host "Started OK: $Url"
    Start-Process $Url
} else {
    Write-Host "Start FAILED: app.py did not come up. Please check app.py manually."
}
