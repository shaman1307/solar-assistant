# Run Solar Smart locally (Windows). Uses sa-config.yaml (+ optional sa-config.local.yaml overlay).
#
# Usage:
#   .\scripts\run-local.ps1
#   .\scripts\run-local.ps1 -NoTunnel

param(
    [switch]$NoTunnel
)

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Virtual env not found. One-time setup:"
    Write-Host "  py -m venv .venv"
    Write-Host "  .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$mainCfg = Join-Path $Root "sa-config.yaml"
if (-not (Test-Path $mainCfg)) {
    Write-Host "Missing sa-config.yaml — copy sa-config.yaml.example and edit."
    exit 1
}

$key = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
$env:INFLUXDB_URL = "http://127.0.0.1:8086"

function Test-LocalPort($Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $Port)
        $c.Close()
        return $true
    } catch {
        return $false
    }
}

$tunnelJob = $null
if (-not $NoTunnel) {
    if (-not (Test-LocalPort 8086)) {
        Write-Host "Starting SSH tunnel 127.0.0.1:8086 -> Pi Influx ..."
        $tunnelJob = Start-Job -ScriptBlock {
            param($KeyPath)
            ssh -N -L 8086:127.0.0.1:8086 -i $KeyPath -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 solar-assistant
        } -ArgumentList $key
        Start-Sleep -Seconds 2
        if (-not (Test-LocalPort 8086)) {
            Write-Host "Tunnel failed. Run manually:"
            Write-Host "  ssh -N -L 8086:127.0.0.1:8086 solar-assistant"
            if ($tunnelJob) { Stop-Job $tunnelJob; Remove-Job $tunnelJob }
            exit 1
        }
        Write-Host "Influx tunnel OK."
    } else {
        Write-Host "Influx tunnel already active on :8086."
    }
}

if (Test-Path (Join-Path $Root ".smart-deployed")) {
    Remove-Item (Join-Path $Root ".smart-deployed") -Force -ErrorAction SilentlyContinue
}

& (Join-Path $PSScriptRoot "stop-local-smart.ps1")

Write-Host "http://127.0.0.1:8000/debug"
Write-Host ""

try {
    & $python -m uvicorn src.main:app --reload --host 127.0.0.1 --port 8000
} finally {
    if ($tunnelJob) {
        Stop-Job $tunnelJob -ErrorAction SilentlyContinue
        Remove-Job $tunnelJob -Force -ErrorAction SilentlyContinue
    }
}
