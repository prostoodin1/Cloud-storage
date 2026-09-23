param(
  [Parameter(Mandatory=$true)][string]$Dist,
  [string]$ExpectedVersion = '0.10.2'
)
$ErrorActionPreference = 'Stop'
$releaseDist = (Resolve-Path -LiteralPath $Dist).Path
$qaRoot = Join-Path (Split-Path $releaseDist -Parent) ('drive-qa-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qaRoot | Out-Null

function Get-FreeQaPort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
  $listener.Start()
  try { return $listener.LocalEndpoint.Port }
  finally { $listener.Stop() }
}

$letter = @('W','V','U','T','S','R','Q','P','O','N','M','L','K','J','I','H','G','F','E','D') |
  Where-Object { -not (Test-Path -LiteralPath ($_ + ':\')) } | Select-Object -First 1
if (-not $letter) { throw 'Нет свободной буквы для изолированной проверки диска' }

$port = Get-FreeQaPort
$baseUrl = "http://127.0.0.1:$port"
$env:CLOUD_STORAGE_CORE_DATA_DIR = Join-Path $qaRoot 'core'
$env:CLOUD_STORAGE_CORE_PORT = [string]$port
$env:CLOUD_STORAGE_LAN_ENABLED = '1'
$env:CLOUD_STORAGE_LAN_PORT = [string](Get-FreeQaPort)
$env:CLOUD_STORAGE_DISCOVERY_PORT = [string](Get-FreeQaPort)
$env:CLOUD_STORAGE_LEGACY_PORT = [string](Get-FreeQaPort)
$env:CLOUD_STORAGE_LEGACY_CORE = Join-Path $releaseDist 'CloudStorageLegacyCore\CloudStorageLegacyCore.exe'
$corePath = Join-Path $releaseDist 'CloudStorageLegacyCore\CloudStorageLegacyCore.exe'
$drivePath = Join-Path $releaseDist 'CloudStorageClient\CloudStorageDrive.exe'
$coreOut = Join-Path $qaRoot 'core-stdout.log'
$coreErr = Join-Path $qaRoot 'core-stderr.log'
$core = Start-Process -FilePath $corePath -WindowStyle Hidden -RedirectStandardOutput $coreOut -RedirectStandardError $coreErr -PassThru
$drive = $null
$checks = [ordered]@{}
try {
  $health = $null
  $deadline = (Get-Date).AddSeconds(30)
  do {
    Start-Sleep -Milliseconds 200
    try { $health = Invoke-RestMethod -Uri "$baseUrl/v1/health" -TimeoutSec 2 }
    catch { $health = $null }
  } until ($health -or $core.HasExited -or (Get-Date) -ge $deadline)
  if (-not $health) { throw "Core не запустился: $(Get-Content -LiteralPath $coreErr -Raw -ErrorAction SilentlyContinue)" }
  $checks['core_version'] = ($health.version -eq $ExpectedVersion)
  $secrets = Get-Content -LiteralPath (Join-Path $env:CLOUD_STORAGE_CORE_DATA_DIR 'core-secrets.json') -Raw | ConvertFrom-Json
  $managerHeaders = @{Authorization=('Bearer ' + $secrets.manager_token)}
  $code = Invoke-RestMethod -Uri "$baseUrl/v1/admin/dynamic-pairing-code" -Headers $managerHeaders
  $paired = Invoke-RestMethod -Method Post -Uri "$baseUrl/v1/pairing/redeem" -ContentType 'application/json' -Body (@{code=$code.code;device_name='PowerShell Drive QA';platform='Windows'} | ConvertTo-Json)
  $deviceHeaders = @{Authorization=('Bearer ' + $paired.device_token)}
  $spaces = @(Invoke-RestMethod -Uri "$baseUrl/v1/spaces" -Headers $deviceHeaders)
  $spaceId = [string]$spaces[0].id
  $userId = [string]$paired.device.user_id
  Invoke-RestMethod -Method Patch -Uri "$baseUrl/v1/admin/users/$userId" -Headers $managerHeaders -ContentType 'application/json' -Body (@{display_name='Drive QA';quota_gib=3} | ConvertTo-Json) | Out-Null

  $clientData = Join-Path $qaRoot 'client'
  $tokenDir = Join-Path $clientData 'tokens'
  New-Item -ItemType Directory -Path $tokenDir -Force | Out-Null
  @{
    schema_version=4
    active_profile_id='default'
    profiles=@(@{
      profile_id='default';server_url=$baseUrl;server_name='Drive QA';certificate_fingerprint=''
      device_id=[string]$paired.device.id;device_status='trusted';last_space_id=$spaceId
      drive_letter=$letter;drive_letters=@{$spaceId=$letter}
    })
  } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $clientData 'client-settings.json') -Encoding utf8
  Add-Type -AssemblyName System.Security
  $plain = [Text.Encoding]::UTF8.GetBytes([string]$paired.device_token)
  $protected = [Security.Cryptography.ProtectedData]::Protect($plain, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
  [IO.File]::WriteAllBytes((Join-Path $tokenDir 'default.bin'), $protected)

  $drive = Start-Process -FilePath $drivePath -WindowStyle Hidden -ArgumentList @('--data-dir',$clientData,'--profile-id','default','--space-id',$spaceId,'--mount',($letter + ':'),'--parent-pid','0') -PassThru
  $root = $letter + ':\'
  $deadline = (Get-Date).AddSeconds(25)
  do { Start-Sleep -Milliseconds 200 } until ((Test-Path -LiteralPath $root) -or $drive.HasExited -or (Get-Date) -ge $deadline)
  $checks['drive_mounted'] = (Test-Path -LiteralPath $root)
  if (-not $checks['drive_mounted']) { throw "Виртуальный диск не подключён; код процесса $($drive.ExitCode)" }

  $folder = Join-Path $root 'Client folder'
  New-Item -ItemType Directory -Path $folder | Out-Null
  $file = Join-Path $folder 'round-trip.bin'
  $payload = [Text.Encoding]::UTF8.GetBytes(('Cloud Storage real drive QA ' * 4096))
  [IO.File]::WriteAllBytes($file, $payload)
  $downloaded = [IO.File]::ReadAllBytes($file)
  $expectedHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($payload))
  $actualHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($downloaded))
  $checks['folder_created'] = (Test-Path -LiteralPath $folder -PathType Container)
  $checks['file_roundtrip_sha256'] = ($expectedHash -eq $actualHash)
  $entries = @(Invoke-RestMethod -Uri "$baseUrl/v1/spaces/$spaceId/entries?directory=Client%20folder" -Headers $deviceHeaders)
  $checks['server_sees_drive_upload'] = ($entries.Count -eq 1 -and $entries[0].name -eq 'round-trip.bin')
  $volume = [IO.DriveInfo]::new($root)
  $checks['quota_is_3_gib'] = ($volume.TotalSize -eq 3GB)
  $checks['usage_recorded'] = ((Invoke-RestMethod -Uri "$baseUrl/v1/spaces" -Headers $deviceHeaders)[0].used_bytes -eq $payload.Length)
} finally {
  if ($drive -and -not $drive.HasExited) {
    Stop-Process -Id $drive.Id -Force -ErrorAction SilentlyContinue
    $drive.WaitForExit(10000) | Out-Null
  }
  try { Invoke-RestMethod -Method Post -Uri "$baseUrl/v1/admin/shutdown" -Headers $managerHeaders -TimeoutSec 5 | Out-Null } catch {}
  if (-not $core.HasExited) {
    if (-not $core.WaitForExit(10000)) { Stop-Process -Id $core.Id -Force -ErrorAction SilentlyContinue }
  }
  $report = Join-Path $qaRoot 'results.json'
  $checks | ConvertTo-Json | Set-Content -LiteralPath $report -Encoding utf8
  $checks | ConvertTo-Json
  Write-Output "REPORT $report"
}
if ($checks.Values -contains $false) { exit 1 }
