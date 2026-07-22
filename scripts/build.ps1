param(
    [ValidateSet('all', 'developer', 'commercial')][string]$Edition = 'all',
    [switch]$SkipInstall,
    [switch]$VerifyLaunch,
    [string]$VerifyReportsDirectory = ""
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
if (-not $SkipInstall) {
    & $Python -m pip install -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build dependencies.' }
}

$Commit = (& git -C $Root rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Commit) { throw 'Unable to determine the current git commit.' }
$Editions = if ($Edition -eq 'all') { @('developer', 'commercial') } else { @($Edition) }
$Dist = [System.IO.Path]::GetFullPath((Join-Path $Root 'dist'))
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

foreach ($CurrentEdition in $Editions) {
    $IsDeveloper = $CurrentEdition -eq 'developer'
    $AppName = if ($IsDeveloper) { 'CreatorAssistant-Developer' } else { 'CreatorAssistant' }
    $ExeName = if ($IsDeveloper) { 'CreatorAssistant-Developer.exe' } else { 'CreatorAssistant.exe' }
    $ShortcutName = if ($IsDeveloper) { 'Creator Assistant Developer' } else { 'Creator Assistant' }
    $Target = [System.IO.Path]::GetFullPath((Join-Path $Dist $AppName))
    $TargetExe = Join-Path $Target $ExeName
    $Staging = [System.IO.Path]::GetFullPath((Join-Path $Dist ('.staging-' + $CurrentEdition)))
    if (-not $Target.StartsWith($Dist, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe build output path.' }
    $Running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { [System.IO.Path]::GetFullPath($_.Path) -eq $TargetExe } catch { $false }
    })
    if ($Running.Count -gt 0) { throw "$ExeName сейчас запущен. PID: $($Running.Id -join ', ')" }
    if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }

    $Generated = Join-Path $Root 'build\generated'
    New-Item -ItemType Directory -Force -Path $Generated | Out-Null
    $BuildInfo = [ordered]@{
        commit = $Commit
        build_date = (Get-Date).ToUniversalTime().ToString('o')
        edition = $CurrentEdition
        version = '0.1.0'
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $Generated 'build_info.json'), $BuildInfo, $Utf8NoBom)

    $PreviousEdition = $env:CREATOR_ASSISTANT_EDITION
    $env:CREATOR_ASSISTANT_EDITION = $CurrentEdition
    Push-Location $Root
    try {
        & $Python -m PyInstaller --noconfirm --clean --distpath $Staging (Join-Path $Root 'CreatorAssistant.spec')
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $CurrentEdition." }
    } finally {
        Pop-Location
        $env:CREATOR_ASSISTANT_EDITION = $PreviousEdition
    }

    $StagedApp = Join-Path $Staging $AppName
    $StagedExe = Join-Path $StagedApp $ExeName
    if (-not (Test-Path -LiteralPath $StagedExe)) { throw "EXE was not found: $StagedExe" }
    if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
    Move-Item -LiteralPath $StagedApp -Destination $Target
    if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }

    & (Join-Path $PSScriptRoot 'verify-package-edition.ps1') -Edition $CurrentEdition `
        -Executable $TargetExe -Python $Python
    if ($LASTEXITCODE -ne 0) { throw "Package boundary verification failed for $CurrentEdition." }

    $Report = ""
    if ($VerifyReportsDirectory) {
        $ReportRoot = [System.IO.Path]::GetFullPath($VerifyReportsDirectory)
        New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null
        $Report = Join-Path $ReportRoot ($CurrentEdition + '-smoke.json')
        if (Test-Path -LiteralPath $Report) { Remove-Item -LiteralPath $Report }
    }
    $Arguments = if ($Report) { '"--verify-edition=' + $Report + '"' } elseif ($VerifyLaunch) { '"--smoke-test"' } else { '' }
    & (Join-Path $PSScriptRoot 'update-desktop-shortcut.ps1') -TargetPath $TargetExe -CommitHash $Commit `
        -ShortcutName $ShortcutName -Launch:($VerifyLaunch -or [bool]$Report) -LaunchArguments $Arguments
    if ($LASTEXITCODE -ne 0) { throw "Desktop shortcut update failed for $CurrentEdition." }
    if ($Report) {
        for ($Attempt = 0; $Attempt -lt 60 -and -not (Test-Path -LiteralPath $Report); $Attempt++) { Start-Sleep -Milliseconds 500 }
        if (-not (Test-Path -LiteralPath $Report)) { throw "Smoke report was not created: $Report" }
        $Verification = Get-Content -Raw -LiteralPath $Report | ConvertFrom-Json
        if (-not $Verification.success) { throw "Packaged smoke test failed: $Report" }
        Write-Host "Packaged $CurrentEdition verified: $Report"
    }
    Write-Host "Ready [$CurrentEdition]: $TargetExe"
}
Write-Host "Commit: $Commit"
