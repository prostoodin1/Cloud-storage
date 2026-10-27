param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Repository = 'prostoodin1/Cloud-storage',
    [string]$Python = '',
    [ValidateRange(0,30)][int]$RetryDelaySeconds = 2
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?$' -or $Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Invalid release identity' }
$tag = "v$Version"
function Invoke-RepairGh {
    param([string[]]$Arguments)
    for ($attempt=1; $attempt -le 3; $attempt++) {
        $result = & gh @Arguments
        if ($LASTEXITCODE -eq 0) { return $result }
        if ($attempt -lt 3) { Start-Sleep -Seconds $RetryDelaySeconds }
    }
    throw "GitHub command failed after retries: $($Arguments[0]) $($Arguments[1])"
}
$release = Invoke-RepairGh -Arguments @('release','view',$tag,'--repo',$Repository,'--json','isDraft,isPrerelease') | ConvertFrom-Json
if ($release.isDraft -or -not $release.isPrerelease) { throw 'Catalog repair requires an already published prerelease' }
$hostTag = (Invoke-RepairGh -Arguments @('release','view','--repo',$Repository,'--json','tagName','--jq','.tagName') | Out-String).Trim()
if (-not $hostTag -or $hostTag -eq $tag) { throw 'A separate stable catalog host is required' }
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('cloud-storage-feed-repair-' + [guid]::NewGuid().ToString('N'))
$recovered = Join-Path $temporary 'release'
$existing = Join-Path $temporary 'existing'
$verified = Join-Path $temporary 'verified'
New-Item -ItemType Directory -Force -Path $recovered,$existing,$verified | Out-Null
try {
    Invoke-RepairGh -Arguments @('release','download',$tag,'--repo',$Repository,'--pattern','cloud-storage-*-catalog.json','--dir',$recovered) | Out-Null
    Invoke-RepairGh -Arguments @('release','download',$tag,'--repo',$Repository,'--pattern',"CloudStorage-*-Setup-$Version-windows-x64.exe",'--dir',$recovered) | Out-Null
    Invoke-RepairGh -Arguments @('release','download',$hostTag,'--repo',$Repository,'--pattern','cloud-storage-*-catalog.json','--dir',$existing) | Out-Null
    & $Python (Join-Path $PSScriptRoot 'verify-release-catalogs.py') --release $recovered --existing $existing --repository $Repository --version $Version
    if ($LASTEXITCODE -ne 0) { throw 'Catalog validation failed; no feed files were changed' }
    foreach ($product in @('client','server')) {
        $name = "cloud-storage-$product-catalog.json"
        $file = Join-Path $recovered $name
        if ((Get-FileHash -LiteralPath $file).Hash -eq (Get-FileHash -LiteralPath (Join-Path $existing $name)).Hash) { continue }
        Invoke-RepairGh -Arguments @('release','upload',$hostTag,'--repo',$Repository,'--clobber',$file) | Out-Null
    }
    Invoke-RepairGh -Arguments @('release','download',$hostTag,'--repo',$Repository,'--pattern','cloud-storage-*-catalog.json','--dir',$verified) | Out-Null
    foreach ($product in @('client','server')) {
        $name = "cloud-storage-$product-catalog.json"
        if ((Get-FileHash -LiteralPath (Join-Path $recovered $name)).Hash -ne (Get-FileHash -LiteralPath (Join-Path $verified $name)).Hash) { throw "Published feed verification failed: $name" }
    }
    $currentHost = (Invoke-RepairGh -Arguments @('release','view','--repo',$Repository,'--json','tagName','--jq','.tagName') | Out-String).Trim()
    if ($currentHost -ne $hostTag) { throw 'Stable release changed during repair; rerun recovery against its new feed' }
    Write-Host "Verified client and server feeds on $hostTag for $tag"
} finally {
    $resolved = (Resolve-Path -LiteralPath $temporary).Path
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar))
    if ((Split-Path -Parent $resolved) -ne $parent -or (Split-Path -Leaf $resolved) -notmatch '^cloud-storage-feed-repair-[0-9a-f]{32}$') { throw 'Unexpected repair directory; cleanup refused' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
