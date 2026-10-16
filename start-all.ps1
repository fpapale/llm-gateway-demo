<#
.SYNOPSIS
  Avvia con un comando l'intero ambiente di prova: 2 gateway, 2 riconoscitori e la demo web (http://localhost:8090).

.DESCRIPTION
  D:\llm-gateway e' di SOLA LETTURA: qui viene solo letto. Per avviarlo si compila una copia di lavoro in
  .runtime\llm-gateway e il suo riconoscitore spaCy gira dal venv originale con PYTHONDONTWRITEBYTECODE=1.
  Le chiavi ES256 di sviluppo stanno in keys\ e si passano ai gateway con la proprieta'
  gateway.auth.public-key-location (nessun file copiato nelle loro cartelle config).
  Il fornitore LLM dei due gateway e' il finto OpenAI della demo (http://127.0.0.1:8090/mock-openai).

  Parametri: -SkipOriginal (non avvia llm-gateway/spaCy), -NoBrowser.
#>
param(
    [switch]$SkipOriginal,
    [switch]$NoBrowser,
    [string]$OriginalDir = 'D:\llm-gateway',
    [string]$NerGatewayDir = 'D:\llm-rizzo-gateway'
)
$ErrorActionPreference = 'Stop'
$Demo = $PSScriptRoot
$Logs = Join-Path $Demo 'logs'
$Runtime = Join-Path $Demo '.runtime'
$Keys = Join-Path $Demo 'keys'
$Pids = Join-Path $Logs 'pids.txt'
New-Item -ItemType Directory -Force $Logs, $Runtime | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function Test-Port([int]$port) {
    $null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

function Start-Service([string]$name, [int]$port, [string]$exe, [string[]]$arguments, [string]$workDir) {
    if (Test-Port $port) { Write-Host "  $name gia' attivo su :$port" -ForegroundColor Yellow; return }
    $outLog = Join-Path $Logs "$name.out.log"
    $errLog = Join-Path $Logs "$name.err.log"
    $quotedArgs = ($arguments | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    $cmdLine = "`"$exe`" $quotedArgs 1>`"$outLog`" 2>`"$errLog`""
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = "cmd.exe /c `"$cmdLine`""
        CurrentDirectory = $workDir
    }
    Add-Content $Pids "$name=$($res.ProcessId)"
    Write-Host "  $name avviato (PID $($res.ProcessId), porta $port)"
}

function Wait-Url([string]$name, [string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-RestMethod $url -TimeoutSec 2 | Out-Null; Write-Host "  $name pronto" -ForegroundColor Green; return }
        catch { Start-Sleep -Seconds 2 }
    }
    Write-Host "  $name NON risponde entro $seconds s: vedere logs\$name.err.log" -ForegroundColor Red
}

function Get-Jar([string]$dir) {
    Get-ChildItem (Join-Path $dir 'target') -Filter '*.jar' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike '*.original' -and $_.Name -notlike '*-plain.jar' } | Select-Object -First 1
}

Remove-Item $Pids -ErrorAction SilentlyContinue

Step 'Ambiente Python della demo e chiavi ES256'
$demoPython = Join-Path $Demo '.venv\Scripts\python.exe'
if (-not (Test-Path $demoPython)) {
    python -m venv (Join-Path $Demo '.venv')
    & $demoPython -m pip install -q -r (Join-Path $Demo 'requirements.txt')
}
& $demoPython -c "import app" # crea keys\ alla prima esecuzione
Write-Host "  chiavi in $Keys"
$publicKey = 'file:' + ((Join-Path $Keys 'client-public.pem') -replace '\\', '/')

$gatewayArgs = @(
    '--server.address=127.0.0.1',
    '--gateway.provider.api-key=demo-key',
    '--gateway.provider.base-url=http://127.0.0.1:8090/mock-openai',
    "--gateway.auth.public-key-location=$publicKey",
    '--gateway.ner.timeout=30s'
)

if (-not $SkipOriginal) {
    Step 'llm-gateway originale (sola lettura: si compila una copia in .runtime)'
    $copy = Join-Path $Runtime 'llm-gateway'
    if (-not (Get-Jar $copy)) {
        Remove-Item $copy -Recurse -Force -ErrorAction SilentlyContinue
        robocopy $OriginalDir $copy /E /XD .git target .superpowers .venv __pycache__ .pytest_cache (Join-Path $OriginalDir 'ner') (Join-Path $OriginalDir 'config') /NFL /NDL /NJH /NJS /NP | Out-Null
        Push-Location $copy
        try { & .\mvnw.cmd -q package -DskipTests; if ($LASTEXITCODE) { throw 'Build di llm-gateway fallita' } } finally { Pop-Location }
    }
    $origPython = Join-Path $OriginalDir 'ner\.venv\Scripts\python.exe'
    if (-not (Test-Path $origPython)) { throw "Manca ${origPython}: creare il venv di llm-gateway/ner (vedi il suo README)" }
    Start-Service 'llm-gateway-ner' 8097 $origPython @('-m', 'uvicorn', 'app:app', '--app-dir', (Join-Path $OriginalDir 'ner'), '--host', '127.0.0.1', '--port', '8097', '--no-access-log') $Demo
    Start-Service 'llm-gateway' 8095 'java' (@('-jar', (Get-Jar $copy).FullName, '--server.port=8095', '--gateway.ner.base-url=http://127.0.0.1:8097',
        '--gateway.policies.enabled-ids=dentalcare-copilot-admin,dentalcare-smart-error,dentalcare-clinical-copilot') + $gatewayArgs) $Runtime
}

Step 'llm-ner-gateway (Rizzo PII 0.3B / DentalCare)'
if (-not (Get-Jar $NerGatewayDir)) {
    Push-Location $NerGatewayDir
    try { & .\mvnw.cmd -q package -DskipTests; if ($LASTEXITCODE) { throw 'Build di llm-ner-gateway fallita' } } finally { Pop-Location }
}
$nerPython = Join-Path $NerGatewayDir 'ner\.venv\Scripts\python.exe'
if (-not (Test-Path $nerPython)) {
    python -m venv (Join-Path $NerGatewayDir 'ner\.venv')
    & $nerPython -m pip install -q -r (Join-Path $NerGatewayDir 'ner\requirements.txt') -c (Join-Path $NerGatewayDir 'ner\constraints.txt') `
        --extra-index-url https://download.pytorch.org/whl/cpu
}
Start-Service 'llm-ner-gateway-ner' 8098 $nerPython @('-m', 'uvicorn', 'app:app', '--app-dir', (Join-Path $NerGatewayDir 'ner'), '--host', '127.0.0.1', '--port', '8098', '--no-access-log') $Demo
Start-Service 'llm-ner-gateway' 8096 'java' (@('-jar', (Get-Jar $NerGatewayDir).FullName, '--server.port=8096', '--gateway.ner.base-url=http://127.0.0.1:8098',
    '--gateway.policies.enabled-ids=dentalcare-copilot-admin,dentalcare-smart-error,dentalcare-clinical-copilot,legal-copilot') + $gatewayArgs) $Runtime

Step 'Demo web (:8090)'
Start-Service 'llm-gateway-demo' 8090 $demoPython @('app.py') $Demo

Step 'Attesa dei servizi (il primo avvio di Rizzo scarica il modello se assente)'
Wait-Url 'llm-gateway-demo' 'http://127.0.0.1:8090/api/config' 30
if (-not $SkipOriginal) {
    Wait-Url 'llm-gateway-ner' 'http://127.0.0.1:8097/health' 120
    Wait-Url 'llm-gateway' 'http://127.0.0.1:8095/actuator/health' 90
}
Wait-Url 'llm-ner-gateway-ner' 'http://127.0.0.1:8098/health' 900
Wait-Url 'llm-ner-gateway' 'http://127.0.0.1:8096/actuator/health' 90

Write-Host "`nDemo pronta: http://localhost:8090   (stop-all.ps1 per fermare tutto)" -ForegroundColor Green
if (-not $NoBrowser) { Start-Process 'http://localhost:8090' }
