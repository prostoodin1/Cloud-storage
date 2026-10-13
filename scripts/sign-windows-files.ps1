param(
  [Parameter(Mandatory=$true)][string[]]$Paths,
  [switch]$Required
)
$ErrorActionPreference = 'Stop'

$thumbprint = ($env:CLOUD_STORAGE_SIGN_CERT_THUMBPRINT -replace '\s', '').ToUpperInvariant()
if (-not $thumbprint) {
  if ($Required -or $env:CLOUD_STORAGE_REQUIRE_CODE_SIGNING -eq '1') {
    throw 'CLOUD_STORAGE_SIGN_CERT_THUMBPRINT is required for this release build.'
  }
  Write-Warning 'Windows code signing skipped: CLOUD_STORAGE_SIGN_CERT_THUMBPRINT is not configured.'
  return
}
if ($thumbprint -notmatch '^[0-9A-F]{40,128}$') {
  throw 'CLOUD_STORAGE_SIGN_CERT_THUMBPRINT has an invalid format.'
}

$signTool = $env:CLOUD_STORAGE_SIGNTOOL
if (-not $signTool) {
  $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($command) { $signTool = $command.Source }
}
if (-not $signTool) {
  $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
  if (Test-Path -LiteralPath $kits) {
    $signTool = Get-ChildItem -LiteralPath $kits -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -like '*\x64\signtool.exe' } |
      Sort-Object FullName -Descending |
      Select-Object -First 1 -ExpandProperty FullName
  }
}
if (-not $signTool -or -not (Test-Path -LiteralPath $signTool)) {
  throw 'signtool.exe was not found. Set CLOUD_STORAGE_SIGNTOOL or install the Windows SDK.'
}

$timestampUrl = if ($env:CLOUD_STORAGE_TIMESTAMP_URL) {
  $env:CLOUD_STORAGE_TIMESTAMP_URL
} else {
  'http://timestamp.digicert.com'
}
$files = foreach ($path in $Paths) {
  $item = Get-Item -LiteralPath $path -ErrorAction Stop
  if ($item.PSIsContainer) {
    Get-ChildItem -LiteralPath $item.FullName -File -Recurse |
      Where-Object { $_.Extension -in @('.exe', '.dll') }
  } else {
    $item
  }
}
$files = @($files | Sort-Object FullName -Unique)
if ($files.Count -eq 0) { throw 'No Windows binaries were provided for code signing.' }

foreach ($file in $files) {
  & $signTool sign /sha1 $thumbprint /fd SHA256 /tr $timestampUrl /td SHA256 $file.FullName
  if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed: $($file.FullName)" }
  & $signTool verify /pa /all $file.FullName
  if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed: $($file.FullName)" }
}
Write-Host "Authenticode verified for $($files.Count) file(s)."
