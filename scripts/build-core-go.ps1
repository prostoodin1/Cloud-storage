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
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$python = if ($env:CLOUD_STORAGE_PYTHON) {
    (Resolve-Path -LiteralPath $env:CLOUD_STORAGE_PYTHON).Path
} elseif (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$version = (& $python -c "from cloud_storage import __version__; print(__version__)").Trim()
if (-not $version) { throw 'Cloud Storage version is empty.' }
$coreDirectory = Join-Path $distRoot 'CloudStorageServerCore'
$serviceDirectory = Join-Path $distRoot 'CloudStorageServerService'
New-Item -ItemType Directory -Path $coreDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $serviceDirectory -Force | Out-Null

Push-Location $source
try {
    & $go test ./...
    if ($LASTEXITCODE -ne 0) { throw 'Go Core tests failed.' }
    $output = Join-Path $coreDirectory 'CloudStorageServerCore.exe'
    $linkerFlags = "-s -w -H=windowsgui -X main.coreVersion=$version"
    & $go build -trimpath -ldflags $linkerFlags -o $output .
    if ($LASTEXITCODE -ne 0) { throw 'Go Core build failed.' }
    $reportedVersion = (& $output --version | Out-String).Trim()
    if ($reportedVersion -ne $version) {
        throw "Go Core version mismatch: built '$reportedVersion', expected '$version'."
    }
    Copy-Item -LiteralPath $output -Destination (Join-Path $serviceDirectory 'CloudStorageServerService.exe') -Force
} finally {
    Pop-Location
}

Write-Host "Native Go Core ready: $coreDirectory"
