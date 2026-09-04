param(
  [Parameter(Mandatory=$true)][string]$Dist,
  [string]$InstallerDirectory = ''
)
$ErrorActionPreference = 'Stop'
$releaseDist = (Resolve-Path -LiteralPath $Dist).Path
$qaRoot = Join-Path $env:TEMP ('cloud-storage-package-qa-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qaRoot | Out-Null
$env:CLOUD_STORAGE_CORE_DATA_DIR = Join-Path $qaRoot 'core'
$env:CLOUD_STORAGE_CORE_PORT = '27865'
$env:CLOUD_STORAGE_LEGACY_PORT = '27875'
$env:CLOUD_STORAGE_LEGACY_CORE = Join-Path $releaseDist 'CloudStorageLegacyCore\CloudStorageLegacyCore.exe'
$env:CLOUD_STORAGE_CONFIG_DIR = Join-Path $qaRoot 'manager'
$env:CLOUD_STORAGE_CLIENT_DATA_DIR = Join-Path $qaRoot 'client'
$env:LOCALAPPDATA = Join-Path $qaRoot 'localappdata'
$env:QT_QPA_PLATFORM = 'offscreen'
$checks = [ordered]@{}
$managerPath = Join-Path $releaseDist 'CloudStorageServerManager\CloudStorageServerManager.exe'
$containerManagerPath = Join-Path $releaseDist 'CloudStorageContainerManager\CloudStorageContainerManager.exe'
$checks['manager_package'] = (
  (Test-Path -LiteralPath $managerPath) -and
  (Test-Path -LiteralPath (Join-Path $releaseDist 'CloudStorageServerManager\_internal\PySide6\Qt6Widgets.dll'))
)
$checks['container_manager_package'] = (
  (Test-Path -LiteralPath $containerManagerPath) -and
  (Test-Path -LiteralPath (Join-Path $releaseDist 'CloudStorageContainerManager\_internal\PySide6\Qt6Widgets.dll'))
)
$checks['client_drive_module'] = Test-Path -LiteralPath (Join-Path $releaseDist 'CloudStorageClient\CloudStorageDrive.exe')
$foreignIcu = @('CloudStorageClient', 'CloudStorageServerManager', 'CloudStorageContainerManager') |
  Where-Object { Test-Path -LiteralPath (Join-Path $releaseDist ($_ + '\_internal\icuuc.dll')) }
$checks['system_icu_not_shadowed'] = ($foreignIcu.Count -eq 0)
# Server Manager executables intentionally request administrator rights. Launching
# them here would display a UAC prompt and make unattended release verification
# impossible; their real widget constructors are exercised by the source smoke
# tests. The non-elevated desktop client is executed from the packaged directory.
$binaries = @(
  @{ Name='client'; Path=(Join-Path $releaseDist 'CloudStorageClient\CloudStorageClient.exe') }
)
if ($InstallerDirectory) {
  $installerRoot = (Resolve-Path -LiteralPath $InstallerDirectory).Path
  $binaries += @(
    @{ Name='client_installer'; Path=(Join-Path $installerRoot 'CloudStorage-Client-Installer-windows-x64.exe') },
    @{ Name='server_installer'; Path=(Join-Path $installerRoot 'CloudStorage-Server-Installer-windows-x64.exe') }
  )
}
foreach ($item in $binaries) {
  $process = Start-Process -FilePath $item.Path -ArgumentList '--smoke-test' -WindowStyle Hidden -PassThru
  $exited = $process.WaitForExit(30000)
  $checks[$item.Name] = ($exited -and $process.ExitCode -eq 0)
  if (-not $exited) {
    $process.Kill($true)
    $process.WaitForExit(5000) | Out-Null
  }
}
$corePath = Join-Path $releaseDist 'CloudStorageServerCore\CloudStorageServerCore.exe'
$coreSmoke = Start-Process -FilePath $corePath -ArgumentList '--smoke-test' -WindowStyle Hidden -PassThru
$checks['go_core_binary'] = ($coreSmoke.WaitForExit(10000) -and $coreSmoke.ExitCode -eq 0)
$compatibilityPath = Join-Path $releaseDist 'CloudStorageLegacyCore\CloudStorageLegacyCore.exe'
$coreOut = Join-Path $qaRoot 'core-stdout.txt'
$coreErr = Join-Path $qaRoot 'core-stderr.txt'
$coreProcess = Start-Process -FilePath $compatibilityPath -WindowStyle Hidden -RedirectStandardOutput $coreOut -RedirectStandardError $coreErr -PassThru
try {
  $deadline = (Get-Date).AddSeconds(30)
  do {
    Start-Sleep -Milliseconds 200
    try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:27865/v1/health' -TimeoutSec 2 }
    catch { $health = $null }
  } until ($health -or (Get-Date) -ge $deadline -or $coreProcess.HasExited)
  $checks['packaged_core_api'] = ($health.version -eq '0.9.21')
  if (-not $checks['packaged_core_api']) {
    $detail = if (Test-Path $coreErr) { (Get-Content -LiteralPath $coreErr -Raw) } else { '' }
    throw "Packaged Core did not become healthy (exited=$($coreProcess.HasExited), code=$(if ($coreProcess.HasExited) {$coreProcess.ExitCode} else {'running'})): $detail"
  }
  $root = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:27865/' -TimeoutSec 5
  $asset = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:27865/web/assets/app.js' -TimeoutSec 5
  $checks['packaged_web_assets'] = ($root.Content -like '*Введите код подключения*' -and $asset.Content -like '*loadSpaces*')
  $secrets = Get-Content -LiteralPath (Join-Path $env:CLOUD_STORAGE_CORE_DATA_DIR 'core-secrets.json') -Raw | ConvertFrom-Json
  Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:27865/v1/admin/shutdown' -Headers @{Authorization=('Bearer ' + $secrets.manager_token)} | Out-Null
  if (-not $coreProcess.WaitForExit(10000)) { throw 'Packaged Core did not stop after authenticated shutdown' }
  $checks['clean_shutdown'] = ($coreProcess.ExitCode -eq 0)
} finally {
  if (-not $coreProcess.HasExited) { $coreProcess.Kill(); $coreProcess.WaitForExit() }
}
$checks | ConvertTo-Json
if ($checks.Values -contains $false) { exit 1 }
