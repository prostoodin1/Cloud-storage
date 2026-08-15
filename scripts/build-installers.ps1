$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$version = (& $python -c "from cloud_storage import __version__; print(__version__)").Trim()
$releaseRoot = Join-Path $projectRoot "work\release-$version"
$env:CLOUD_STORAGE_BUILD_DIST = Join-Path $releaseRoot 'dist'
$env:CLOUD_STORAGE_BUILD_WORK = Join-Path $releaseRoot 'build'
$compilerCandidates = @(
    $env:CLOUD_STORAGE_ISCC,
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
)
$compiler = $compilerCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $compiler) {
    throw 'Inno Setup 6 was not found. Install it and run the build again.'
}

& (Join-Path $PSScriptRoot 'build.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
& (Join-Path $PSScriptRoot 'build-drive.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Drive build failed.' }
& (Join-Path $PSScriptRoot 'fetch-packaging-deps.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Packaging dependency download failed.' }

& $compiler "/DBuildRoot=$env:CLOUD_STORAGE_BUILD_DIST" (Join-Path $projectRoot 'packaging\CloudStorageClient.iss')
if ($LASTEXITCODE -ne 0) { throw 'Client installer build failed.' }
& $compiler "/DBuildRoot=$env:CLOUD_STORAGE_BUILD_DIST" (Join-Path $projectRoot 'packaging\CloudStorageServer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Server installer build failed.' }
$outputs = Join-Path $projectRoot 'outputs'
$bootstrapDist = Join-Path $releaseRoot 'installer-dist'
$bootstrapWork = Join-Path $releaseRoot 'installer-build'
$bootstrapSpec = Join-Path $releaseRoot 'installer-spec'
New-Item -ItemType Directory -Path $bootstrapDist, $bootstrapWork, $bootstrapSpec -Force | Out-Null
& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'CloudStorage-Client-Installer-windows-x64' `
    --icon (Join-Path $projectRoot 'assets\cloud-storage.ico') `
    --distpath $bootstrapDist --workpath (Join-Path $bootstrapWork 'client') `
    --specpath $bootstrapSpec `
    (Join-Path $projectRoot 'cloud_storage\installer\client_main.py')
if ($LASTEXITCODE -ne 0) { throw 'Stable client installer build failed.' }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'CloudStorage-Server-Installer-windows-x64' `
    --icon (Join-Path $projectRoot 'assets\cloud-storage.ico') `
    --distpath $bootstrapDist --workpath (Join-Path $bootstrapWork 'server') `
    --specpath $bootstrapSpec `
    (Join-Path $projectRoot 'cloud_storage\installer\server_main.py')
if ($LASTEXITCODE -ne 0) { throw 'Stable server installer build failed.' }
Copy-Item -LiteralPath (Join-Path $bootstrapDist 'CloudStorage-Client-Installer-windows-x64.exe') -Destination $outputs -Force
Copy-Item -LiteralPath (Join-Path $bootstrapDist 'CloudStorage-Server-Installer-windows-x64.exe') -Destination $outputs -Force
$clientZip = Join-Path $outputs "CloudStorage-Desktop-Client-$version-windows-x64.zip"
$serverZip = Join-Path $outputs "CloudStorage-Server-Manager-$version-windows-x64.zip"
Compress-Archive -Path (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageClient') -DestinationPath $clientZip -CompressionLevel Optimal -Force
Compress-Archive -Path @(
    (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageServerManager'),
    (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageContainerManager'),
    (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageServerCore'),
    (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageLegacyCore'),
    (Join-Path $env:CLOUD_STORAGE_BUILD_DIST 'CloudStorageServerService')
) -DestinationPath $serverZip -CompressionLevel Optimal -Force
Write-Host "Stable online installers and versioned setup payloads ready in $projectRoot\outputs"
