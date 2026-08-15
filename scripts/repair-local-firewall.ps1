$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath)
    )
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    exit $process.ExitCode
}

foreach ($name in @(
    'Cloud Storage HTTPS (Private)',
    'Cloud Storage Discovery (Private)',
    'Cloud Storage HTTPS (Local subnet)',
    'Cloud Storage Discovery (Local subnet)'
)) {
    & netsh.exe advfirewall firewall delete rule name="$name" | Out-Null
}

& netsh.exe advfirewall firewall add rule `
    name='Cloud Storage HTTPS (Local subnet)' `
    dir=in action=allow protocol=TCP localport=8766 profile=any remoteip=localsubnet | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Cloud Storage HTTPS firewall rule.' }

& netsh.exe advfirewall firewall add rule `
    name='Cloud Storage Discovery (Local subnet)' `
    dir=in action=allow protocol=UDP localport=47777 profile=any remoteip=localsubnet | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Cloud Storage discovery firewall rule.' }

Write-Output 'Cloud Storage local-subnet firewall rules repaired.'
