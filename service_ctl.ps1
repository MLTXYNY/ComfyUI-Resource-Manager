param([string]$Action = "start")
$ErrorActionPreference = "SilentlyContinue"
$Port = 8000
$Url = "http://127.0.0.1:$Port"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Port {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-Py {
    foreach ($c in @("py", "python", "python3")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
    }
    return $null
}

function Ensure-Deps($pyCmd) {
    & $pyCmd -c "import flask, psutil, PIL" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Missing dependencies, installing from requirements.txt ..."
        & $pyCmd -m pip install -r "$dir\requirements.txt"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Dependency install FAILED. Please run manually: $pyCmd -m pip install -r requirements.txt"
            exit 1
        }
    }
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

$pyCmd = Get-Py
if (-not $pyCmd) {
    Write-Host "Python not found. Please install Python 3.10+ and add it to PATH, then retry."
    exit 1
}

Ensure-Deps $pyCmd

Write-Host "Starting service, please wait..."
Start-Process -FilePath $pyCmd -ArgumentList "app.py" -WorkingDirectory $dir -WindowStyle Hidden
Start-Sleep -Seconds 3

if (Test-Port) {
    Write-Host "Started OK: $Url"
    Start-Process $Url
} else {
    Write-Host "Start FAILED: app.py did not come up. Please run app.py manually to see errors."
}
