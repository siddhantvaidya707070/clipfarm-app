$ErrorActionPreference = 'Stop'
$clipfarmRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $clipfarmRoot
$pythonPath = Join-Path $clipfarmRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pythonPath) {
    & $pythonPath (Join-Path $PSScriptRoot 'sync_secrets.py')
    if ($LASTEXITCODE -ne 0) { throw 'OpenAI credential synchronization failed.' }
}
& (Join-Path $PSScriptRoot 'secure_secrets.ps1')
$dockerPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
if (Test-Path -LiteralPath $dockerPath) {
    Start-Process -FilePath $dockerPath -WindowStyle Hidden
}
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}
docker compose up -d --no-build
if ($LASTEXITCODE -ne 0) { throw 'Clipfarm could not start. Inspect docker compose ps and logs.' }
