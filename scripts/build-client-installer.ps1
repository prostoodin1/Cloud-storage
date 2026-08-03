$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$clientExe = Join-Path $projectRoot 'dist\CloudStorageClient\CloudStorageClient.exe'
$definition = Join-Path $projectRoot 'packaging\CloudStorageClient.iss'
$candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
)
$compiler = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

if (-not (Test-Path -LiteralPath $clientExe)) {
    throw 'Desktop Client build not found. Run scripts\build.ps1 first.'
}
if (-not $compiler) {
    throw 'Inno Setup 6 not found. Install it and run this script again.'
}

& $compiler $definition
if ($LASTEXITCODE -ne 0) { throw 'Client installer build failed.' }
Write-Host "Client installer ready in $projectRoot\outputs"
