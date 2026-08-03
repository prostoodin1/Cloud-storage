$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceExecutable = Join-Path $projectRoot 'dist\CloudStorageServerService\CloudStorageServerService.exe'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}

Stop-Service -Name 'CloudStorageServerCore' -ErrorAction SilentlyContinue
& $serviceExecutable remove
if ($LASTEXITCODE -ne 0) { throw 'Windows service removal failed.' }
Write-Host 'Service removed. Server data in ProgramData\CloudStorage was preserved.'
