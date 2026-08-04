$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Write-Warning 'This compatibility command now builds both complete Stage 7 installers.'
& (Join-Path $PSScriptRoot 'build-installers.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
