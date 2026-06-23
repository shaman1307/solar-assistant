# Stop local Solar Smart (uvicorn on :8000) and optional SSH Influx tunnel jobs.
#
# Usage:
#   .\scripts\stop-local-smart.ps1
#   .\scripts\stop-local-smart.ps1 -IncludeTunnel

param(
    [switch]$IncludeTunnel
)

function Test-SmartUvicornProcess($cmd) {
    return $cmd -match 'src\.main:app' -and $cmd -match '8000'
}

function Get-LocalSmartPids {
    $pids = New-Object System.Collections.Generic.HashSet[int]
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        if (Test-SmartUvicornProcess $_.CommandLine) {
            [void]$pids.Add([int]$_.ProcessId)
        }
    }
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $procId = $_.OwningProcess
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if ($proc -and (Test-SmartUvicornProcess $proc.CommandLine)) {
            [void]$pids.Add([int]$procId)
        }
    }
    return @($pids)
}

$targets = Get-LocalSmartPids
if (-not $targets.Count) {
    Write-Host "No local uvicorn on :8000."
} else {
    foreach ($procId in $targets) {
        Write-Host "Stopping PID $procId ..."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    $left = Get-LocalSmartPids
    if ($left.Count) {
        Write-Host "Warning: still running: $($left -join ', ')"
    } else {
        Write-Host "Port 8000 cleared."
    }
}

if ($IncludeTunnel) {
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match '8086:127\.0\.0\.1:8086'
    } | ForEach-Object {
        Write-Host "Stopping SSH tunnel PID $($_.ProcessId) ..."
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
