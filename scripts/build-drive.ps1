$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$distRoot = if ($env:CLOUD_STORAGE_BUILD_DIST) { $env:CLOUD_STORAGE_BUILD_DIST } else { Join-Path $projectRoot 'dist' }
$go = $env:CLOUD_STORAGE_GO
if (-not $go) {
    $go = (Get-Command go -ErrorAction SilentlyContinue).Source
}
if (-not $go) {
    throw 'Go 1.25 or newer was not found. Install Go and run the build again.'
}
$driveSource = Join-Path $projectRoot 'drive_windows'
$output = Join-Path $distRoot 'CloudStorageDrive.exe'
$clientOutput = Join-Path $distRoot 'CloudStorageClient\CloudStorageDrive.exe'

Push-Location $driveSource
try {
    & $go test ./...
    if ($LASTEXITCODE -ne 0) { throw 'Windows drive tests failed.' }
    & $go build -trimpath -ldflags '-s -w -H=windowsgui' -o $output .
    if ($LASTEXITCODE -ne 0) { throw 'Windows drive build failed.' }
} finally {
    Pop-Location
}
Copy-Item -LiteralPath $output -Destination $clientOutput -Force
Write-Host "Windows drive ready: $output"
