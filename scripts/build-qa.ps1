param([string]$Name = 'qa-2026-09-07')
$ErrorActionPreference = 'Stop'
if ($Name -notmatch '^qa-[0-9-]+$') { throw 'Invalid QA directory name' }
$project = Split-Path -Parent $PSScriptRoot
$qaRoot = Join-Path $project "work\$Name"
$env:CLOUD_STORAGE_BUILD_DIST = Join-Path $qaRoot 'dist'
$env:CLOUD_STORAGE_BUILD_WORK = Join-Path $qaRoot 'build'
$python = $env:CLOUD_STORAGE_PYTHON
$compiler = $env:CLOUD_STORAGE_ISCC
if (-not $python -or -not $compiler) { throw 'Set CLOUD_STORAGE_PYTHON and CLOUD_STORAGE_ISCC' }
New-Item -ItemType Directory -Path $qaRoot -Force | Out-Null
& (Join-Path $PSScriptRoot 'build.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }
& (Join-Path $PSScriptRoot 'build-drive.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Drive build failed' }
& (Join-Path $PSScriptRoot 'fetch-packaging-deps.ps1')
if ($LASTEXITCODE -ne 0) { throw 'WinFsp verification failed' }
$setups = Join-Path $qaRoot 'setups'
New-Item -ItemType Directory -Path $setups -Force | Out-Null
foreach ($product in @('Client', 'Server')) {
    & $compiler '/Qp' "/DBuildRoot=$env:CLOUD_STORAGE_BUILD_DIST" "/O$setups" (Join-Path $project "packaging\CloudStorage$product.iss")
    if ($LASTEXITCODE -ne 0) { throw "$product Setup build failed" }
}
$bootstrapDist = Join-Path $qaRoot 'installers'
$bootstrapWork = Join-Path $qaRoot 'installer-build'
$bootstrapSpec = Join-Path $qaRoot 'installer-spec'
New-Item -ItemType Directory -Path $bootstrapDist, $bootstrapWork, $bootstrapSpec -Force | Out-Null
foreach ($product in @('Client', 'Server')) {
    & $python (Join-Path $PSScriptRoot 'package-app.py') --noconfirm --clean --onefile --windowed `
        --name "CloudStorage-$product-Installer-windows-x64" `
        --icon (Join-Path $project 'assets\cloud-storage.ico') `
        --distpath $bootstrapDist --workpath (Join-Path $bootstrapWork $product) `
        --specpath $bootstrapSpec `
        (Join-Path $project ('cloud_storage\installer\' + $product.ToLower() + '_main.py'))
    if ($LASTEXITCODE -ne 0) { throw "$product bootstrap build failed" }
}
Write-Output "QA_BUILD_READY $qaRoot"
