@echo off
setlocal

cd /d "%~dp0"
set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"

echo Controllo della porta %PORT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$processIds = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); foreach ($processId in $processIds) { Write-Host ('Arresto del processo ' + $processId + ' sulla porta %PORT%'); Stop-Process -Id $processId -Force -ErrorAction Stop }; Start-Sleep -Milliseconds 300; if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { Write-Error 'La porta %PORT% e'' ancora occupata.'; exit 1 }"
if errorlevel 1 (
    echo Impossibile liberare la porta %PORT%.
    exit /b 1
)

echo Avvio dell'applicazione...
start "PhotoOrg server" /b uv run photoorg app --no-window --port %PORT% --strict-port

echo Attendo che il server sia pronto...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline = (Get-Date).AddSeconds(60); do { try { $response = Invoke-WebRequest -UseBasicParsing -Uri '%URL%api/ping' -TimeoutSec 2; if ($response.StatusCode -eq 200) { exit 0 } } catch {} Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo Il server non e' diventato disponibile entro 60 secondi.
    exit /b 1
)

start "" "%URL%"
endlocal
