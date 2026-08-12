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
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageContainerManager.spec')
if ($LASTEXITCODE -ne 0) { throw 'Container Manager build failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageLegacyCore.spec')
if ($LASTEXITCODE -ne 0) { throw 'Compatibility Core build failed.' }
& (Join-Path $PSScriptRoot 'build-core-go.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Native Go Core build failed.' }
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $projectRoot 'packaging\CloudStorageClient.spec')
if ($LASTEXITCODE -ne 0) { throw 'Desktop Client build failed.' }

Write-Host "Build ready: $distRoot"
