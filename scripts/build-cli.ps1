$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = if ($env:CLOUD_STORAGE_PYTHON) { $env:CLOUD_STORAGE_PYTHON } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
$distRoot = if ($env:CLOUD_STORAGE_BUILD_DIST) { $env:CLOUD_STORAGE_BUILD_DIST } else { Join-Path $projectRoot 'dist' }
$workRoot = if ($env:CLOUD_STORAGE_BUILD_WORK) { $env:CLOUD_STORAGE_BUILD_WORK } else { Join-Path $projectRoot 'build' }
& $python (Join-Path $projectRoot 'scripts\package-app.py') --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageCLI.spec')
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
