$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$distRoot = if ($env:CLOUD_STORAGE_BUILD_DIST) { $env:CLOUD_STORAGE_BUILD_DIST } else { Join-Path $projectRoot 'dist' }
$workRoot = if ($env:CLOUD_STORAGE_BUILD_WORK) { $env:CLOUD_STORAGE_BUILD_WORK } else { Join-Path $projectRoot 'build' }

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Virtual environment not found. Create .venv and install .[build] first.'
}

& $python -m pytest
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $python (Join-Path $projectRoot 'scripts\generate_icon.py')
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageServer.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageCore.spec')
if ($LASTEXITCODE -ne 0) { throw 'Server Core build failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageWindowsService.spec')
if ($LASTEXITCODE -ne 0) { throw 'Windows service host build failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageClient.spec')
if ($LASTEXITCODE -ne 0) { throw 'Desktop Client build failed.' }

Write-Host "Build ready: $distRoot"
