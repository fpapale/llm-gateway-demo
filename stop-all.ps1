<#
.SYNOPSIS  Ferma i processi avviati da start-all.ps1 (PID in logs\pids.txt).
#>
$pids = Join-Path $PSScriptRoot 'logs\pids.txt'
if (-not (Test-Path $pids)) { Write-Host 'Nessun processo registrato.'; return }
foreach ($line in Get-Content $pids) {
    $name, $id = $line -split '=', 2
    cmd.exe /c "taskkill /PID $id /T /F >nul 2>&1"
    Write-Host "fermato $name (PID $id)"
}
Remove-Item $pids
