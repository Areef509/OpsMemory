$ErrorActionPreference = 'Stop'
if (-not (Test-Path '.env')) { throw 'Missing .env. Create it from .env.example and configure POSTGRES_PASSWORD first.' }
Get-Content -Raw '.\db\migrations\002_runtime_integrity.sql' | docker compose exec -T db psql -U postgres -d opsmemory -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) { throw 'Migration 002 failed. Do not delete the PostgreSQL volume; inspect the psql error first.' }
Write-Host 'Migration 002 applied successfully.' -ForegroundColor Green
