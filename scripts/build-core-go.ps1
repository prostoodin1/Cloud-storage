$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distRoot = if ($env:CLOUD_STORAGE_BUILD_DIST) { $env:CLOUD_STORAGE_BUILD_DIST } else { Join-Path $projectRoot 'dist' }
$go = $env:CLOUD_STORAGE_GO
if (-not $go) {
    $goCommand = Get-Command go -ErrorAction SilentlyContinue
    if ($goCommand) { $go = $goCommand.Source }
}
if (-not $go) {
    throw 'Go 1.25 or newer was not found. Set CLOUD_STORAGE_GO or install Go.'
}

$source = Join-Path $projectRoot 'core_go'
$coreDirectory = Join-Path $distRoot 'CloudStorageServerCore'
$serviceDirectory = Join-Path $distRoot 'CloudStorageServerService'
New-Item -ItemType Directory -Path $coreDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $serviceDirectory -Force | Out-Null

Push-Location $source
try {
    & $go test ./...
    if ($LASTEXITCODE -ne 0) { throw 'Go Core tests failed.' }
    $output = Join-Path $coreDirectory 'CloudStorageServerCore.exe'
    & $go build -trimpath -ldflags '-s -w -H=windowsgui' -o $output .
    if ($LASTEXITCODE -ne 0) { throw 'Go Core build failed.' }
    Copy-Item -LiteralPath $output -Destination (Join-Path $serviceDirectory 'CloudStorageServerService.exe') -Force
} finally {
    Pop-Location
}

Write-Host "Native Go Core ready: $coreDirectory"
