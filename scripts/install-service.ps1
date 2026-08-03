$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$coreDirectory = Join-Path $projectRoot 'dist\CloudStorageServerCore'
$coreExecutable = Join-Path $coreDirectory 'CloudStorageServerCore.exe'
$serviceExecutable = Join-Path $projectRoot 'dist\CloudStorageServerService\CloudStorageServerService.exe'
$dataDirectory = Join-Path $env:ProgramData 'CloudStorage'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}
if (-not (Test-Path -LiteralPath $coreExecutable) -or -not (Test-Path -LiteralPath $serviceExecutable)) {
    throw 'Build CloudStorageServerCore before installing the service.'
}

New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
$env:CLOUD_STORAGE_CORE_DATA_DIR = $dataDirectory
& $coreExecutable --initialize-only
if ($LASTEXITCODE -ne 0) { throw 'Core initialization failed.' }
& $serviceExecutable --startup auto install
if ($LASTEXITCODE -ne 0) { throw 'Windows service installation failed.' }
Start-Service -Name 'CloudStorageServerCore'
Write-Host 'Cloud Storage Server Core service installed and started.'
