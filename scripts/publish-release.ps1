param(
    [string]$Version = "0.9.19",
    [string]$MobileVersion = "0.9.14",
    [string]$Repository = "prostoodin1/Cloud-storage",
    [string]$OutputDirectory = "",
    [string]$PayloadDirectory = "",
    [string]$PrivateKey = "",
    [string]$NotesFile = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$output = if ($OutputDirectory) { $OutputDirectory } else { Join-Path $projectRoot "output" }
$payloads = if ($PayloadDirectory) { $PayloadDirectory } else { Join-Path $projectRoot "outputs" }
$key = if ($PrivateKey) { $PrivateKey } else { Join-Path (Split-Path $projectRoot -Parent) "private\cloud-storage-update-ed25519.pem" }
$notes = if ($NotesFile) { $NotesFile } else { Join-Path $projectRoot "RELEASE_NOTES_$Version.md" }
$tag = "v$Version"
$targetCommit = (& git -C $projectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $targetCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve the release source commit"
}
$expectedUserFiles = @(
    "CloudStorage-Server-Installer-windows-x64.exe",
    "CloudStorage-Client-Installer-windows-x64.exe",
    "CloudStorage-Desktop-Client-$Version-windows-x64.zip",
    "CloudStorage-Server-Manager-$Version-windows-x64.zip",
    "CloudStorage-Mobile-Client-$MobileVersion-android.apk",
    "CloudStorage-Mobile-Manager-$MobileVersion-android.apk"
)
$payloadNames = @(
    "CloudStorage-Server-Setup-$Version-windows-x64.exe",
    "CloudStorage-Client-Setup-$Version-windows-x64.exe"
)
$catalogNames = @(
    "cloud-storage-client-catalog.json",
    "cloud-storage-server-catalog.json"
)

foreach ($required in @($python, $key, $notes)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required release input is missing: $required"
    }
}
if (-not (Test-Path -LiteralPath $output -PathType Container)) {
    throw "Output directory is missing: $output"
}
foreach ($payload in $payloadNames) {
    $path = Join-Path $payloads $payload
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Versioned setup payload is missing: $path"
    }
}
$actualUserFiles = @(Get-ChildItem -LiteralPath $output -File | Select-Object -ExpandProperty Name)
$unexpected = @($actualUserFiles | Where-Object { $_ -notin $expectedUserFiles })
$missing = @($expectedUserFiles | Where-Object { $_ -notin $actualUserFiles })
if ($unexpected.Count -or $missing.Count -or $actualUserFiles.Count -ne 6) {
    throw "output must contain exactly six $Version user files. Missing: $($missing -join ', '); unexpected: $($unexpected -join ', ')"
}

& gh auth status | Out-Host
if ($LASTEXITCODE -ne 0) { throw "GitHub CLI is not authenticated" }
$existingRelease = & gh release list --repo $Repository --limit 100 --json tagName | ConvertFrom-Json
if ($existingRelease.tagName -contains $tag) { throw "Release $tag already exists" }

$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("cloud-storage-release-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    & gh release download --repo $Repository --pattern "cloud-storage-*-catalog.json" --dir $temporary
    if ($LASTEXITCODE -ne 0) { throw "Existing signed catalogs could not be downloaded" }

    $clientInstaller = Join-Path $payloads "CloudStorage-Client-Setup-$Version-windows-x64.exe"
    $serverInstaller = Join-Path $payloads "CloudStorage-Server-Setup-$Version-windows-x64.exe"
    $baseUrl = "https://github.com/$Repository/releases/download/$tag"
    $clientManifest = Join-Path $temporary "client-$Version.json"
    $serverManifest = Join-Path $temporary "server-$Version.json"
    $releaseText = (Get-Content -LiteralPath $notes -Raw -Encoding utf8).Trim()
    & $python (Join-Path $PSScriptRoot "sign-update-manifest.py") --key $key --product client --version $Version --installer $clientInstaller --url "$baseUrl/$([IO.Path]::GetFileName($clientInstaller))" --notes $releaseText --output $clientManifest
    if ($LASTEXITCODE -ne 0) { throw "Client manifest signing failed" }
    & $python (Join-Path $PSScriptRoot "sign-update-manifest.py") --key $key --product server --version $Version --installer $serverInstaller --url "$baseUrl/$([IO.Path]::GetFileName($serverInstaller))" --notes $releaseText --output $serverManifest
    if ($LASTEXITCODE -ne 0) { throw "Server manifest signing failed" }

    $clientCatalog = Join-Path $temporary $catalogNames[0]
    $serverCatalog = Join-Path $temporary $catalogNames[1]
    $oldClientCatalog = Join-Path $temporary "cloud-storage-client-catalog.json"
    $oldServerCatalog = Join-Path $temporary "cloud-storage-server-catalog.json"
    $oldClientCopy = Join-Path $temporary "old-client-catalog.json"
    $oldServerCopy = Join-Path $temporary "old-server-catalog.json"
    Copy-Item -LiteralPath $oldClientCatalog -Destination $oldClientCopy
    Copy-Item -LiteralPath $oldServerCatalog -Destination $oldServerCopy
    & $python (Join-Path $PSScriptRoot "sign-update-catalog.py") --key $key --product client --existing-catalog $oldClientCopy --manifest $clientManifest --output $clientCatalog
    if ($LASTEXITCODE -ne 0) { throw "Client catalog signing failed" }
    & $python (Join-Path $PSScriptRoot "sign-update-catalog.py") --key $key --product server --existing-catalog $oldServerCopy --manifest $serverManifest --output $serverCatalog
    if ($LASTEXITCODE -ne 0) { throw "Server catalog signing failed" }

    & gh release create $tag --repo $Repository --target $targetCommit --title "Cloud Storage $Version" --notes-file $notes --draft
    if ($LASTEXITCODE -ne 0) { throw "Draft release could not be created" }
    $assets = @($expectedUserFiles | ForEach-Object { Join-Path $output $_ }) + `
        @($payloadNames | ForEach-Object { Join-Path $payloads $_ }) + `
        @($clientCatalog, $serverCatalog)
    & gh release upload $tag --repo $Repository @assets
    if ($LASTEXITCODE -ne 0) { throw "Release asset upload failed; the release remains a draft" }

    $publishedAssets = & gh release view $tag --repo $Repository --json assets | ConvertFrom-Json
    $assetNames = @($publishedAssets.assets | Select-Object -ExpandProperty name)
    $expectedAssets = @($expectedUserFiles + $payloadNames + $catalogNames)
    $missingAssets = @($expectedAssets | Where-Object { $_ -notin $assetNames })
    $extraAssets = @($assetNames | Where-Object { $_ -notin $expectedAssets })
    if ($missingAssets.Count -or $extraAssets.Count -or $assetNames.Count -ne 10) {
        throw "Draft verification failed; release remains private. Missing: $($missingAssets -join ', '); extra: $($extraAssets -join ', ')"
    }
    foreach ($file in $expectedUserFiles) {
        $localSize = (Get-Item -LiteralPath (Join-Path $output $file)).Length
        $remoteSize = [int64](($publishedAssets.assets | Where-Object name -eq $file).size)
        if ($localSize -ne $remoteSize) { throw "Uploaded size mismatch for $file" }
    }
    foreach ($file in $payloadNames) {
        $localSize = (Get-Item -LiteralPath (Join-Path $payloads $file)).Length
        $remoteSize = [int64](($publishedAssets.assets | Where-Object name -eq $file).size)
        if ($localSize -ne $remoteSize) { throw "Uploaded payload size mismatch for $file" }
    }

    & gh release edit $tag --repo $Repository --draft=false --latest
    if ($LASTEXITCODE -ne 0) { throw "Draft is complete but could not be published" }
    Write-Host "Published $tag atomically with six user files, two setup payloads and two signed catalogs."
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
