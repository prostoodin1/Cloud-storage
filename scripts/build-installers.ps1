$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
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

& $compiler (Join-Path $projectRoot 'packaging\CloudStorageClient.iss')
if ($LASTEXITCODE -ne 0) { throw 'Client installer build failed.' }
& $compiler (Join-Path $projectRoot 'packaging\CloudStorageServer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Server installer build failed.' }
Write-Host "Installers ready in $projectRoot\outputs"
