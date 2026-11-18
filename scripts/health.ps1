$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
docker compose ps
Invoke-RestMethod 'http://localhost:8787/health' | ConvertTo-Json
Invoke-RestMethod 'http://localhost:8788/health/db' | ConvertTo-Json
Invoke-RestMethod 'http://localhost:8788/health/worker' | ConvertTo-Json
Invoke-WebRequest 'http://localhost:3107/api/auth/get-session' | Select-Object StatusCode
