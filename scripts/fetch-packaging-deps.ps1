$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$targetDirectory = Join-Path $projectRoot 'packaging\third_party'
$target = Join-Path $targetDirectory 'winfsp-2.1.25156.msi'
$expected = '073A70E00F77423E34BED98B86E600DEF93393BA5822204FAC57A29324DB9F7A'
$uri = 'https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi'

New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
if (-not (Test-Path -LiteralPath $target)) {
    $temporary = $target + '.download'
    Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $temporary
    Move-Item -LiteralPath $temporary -Destination $target
}
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash
if ($actual -ne $expected) {
    throw "WinFsp checksum mismatch. Expected $expected, received $actual."
}
Write-Host "Verified WinFsp 2.1.25156: $actual"
