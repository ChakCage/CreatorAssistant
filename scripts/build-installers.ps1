param(
    [string]$Version = "",
    [ValidateSet('all', 'developer', 'commercial', 'commercial-staging')][string]$Edition = 'all',
    [string]$IsccPath = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
if (-not $Version) {
    $Version = (& $Python -c "import sys;sys.path.insert(0,r'$Root\\src');from creator_assistant.version import __version__;print(__version__)").Trim()
}
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build.ps1') -Edition $Edition -SkipInstall
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
}

if (-not $IsccPath) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $candidates = @(
        $(if ($command) { $command.Source }),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $IsccPath = $candidates | Select-Object -First 1
}
if (-not $IsccPath) {
    throw 'Inno Setup 6 is not installed. Install it from https://jrsoftware.org/isdl.php and rerun this script.'
}

$Profiles = if ($Edition -eq 'all') { @('developer', 'commercial', 'commercial-staging') } else { @($Edition) }
$Output = Join-Path $Root 'dist\installers'
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Spec = Join-Path $Root 'installer\CreatorAssistant.iss'
$WindowsVersion = (($Version -split '[-+]')[0] + '.0')
$Metadata = @{
    'developer' = @('CreatorAssistant-Developer', 'CreatorAssistant-Developer-Setup')
    'commercial' = @('CreatorAssistant', 'CreatorAssistant-Commercial-Setup')
    'commercial-staging' = @('CreatorAssistant-Commercial-Staging', 'CreatorAssistant-Commercial-Staging-Setup')
}
$Artifacts = @()
foreach ($Profile in $Profiles) {
    $Source = Join-Path $Root ('dist\' + $Metadata[$Profile][0])
    if (-not (Test-Path -LiteralPath $Source)) { throw "Package is missing: $Source" }
    & $IsccPath "/DProfile=$Profile" "/DSourceDir=$Source" "/DAppVersion=$Version" `
        "/DWindowsVersion=$WindowsVersion" "/DOutputDir=$Output" $Spec
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed for $Profile." }
    $Installer = Join-Path $Output ($Metadata[$Profile][1] + "-$Version.exe")
    if (-not (Test-Path -LiteralPath $Installer)) { throw "Installer is missing: $Installer" }
    $Artifacts += $Installer
}

$Signed = $false
$Signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if ($env:CREATOR_ASSISTANT_SIGN_CERT_SHA1 -and $Signtool) {
    foreach ($Installer in $Artifacts) {
        & $Signtool.Source sign /sha1 $env:CREATOR_ASSISTANT_SIGN_CERT_SHA1 /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $Installer
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed: $Installer" }
        & $Signtool.Source verify /pa /all $Installer
        if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed: $Installer" }
    }
    $Signed = $true
}

$Lines = foreach ($Installer in $Artifacts) {
    $Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash *$(Split-Path -Leaf $Installer)"
}
[System.IO.File]::WriteAllLines((Join-Path $Output 'SHA256SUMS.txt'), $Lines, (New-Object System.Text.UTF8Encoding($false)))
$Status = if ($Signed) { 'AUTHENTICODE SIGNED' } else { 'UNSIGNED BETA' }
Write-Host $Status
$Artifacts | ForEach-Object { Write-Host $_ }
