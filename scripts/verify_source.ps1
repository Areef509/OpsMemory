$ErrorActionPreference = 'Stop'
$python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Project virtual environment not found at .venv.' }
$env:PYTHONPATH = 'backend'
& $python -m unittest discover -s .\backend\tests\unit -v
if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }
& $python -m unittest discover -s .\backend\tests\integration -v
if ($LASTEXITCODE -ne 0) { throw 'Integration tests failed.' }
& $python -m compileall -q .\backend\app .\scripts
if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }
Write-Host 'Source unit tests, PostgreSQL integration tests, and compilation passed.' -ForegroundColor Green
