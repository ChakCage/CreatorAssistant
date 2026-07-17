param(
    [switch]$SkipInstall,
    [switch]$VerifyLaunch,
    [string]$VerifyUiReport = ""
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

if (-not $SkipInstall) {
    & $Python -m pip install -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build dependencies.' }
}

$Commit = (& git -C $Root rev-parse --short=12 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Commit) { throw 'Unable to determine the current git commit.' }
$Generated = Join-Path $Root 'build\generated'
New-Item -ItemType Directory -Force -Path $Generated | Out-Null
$BuildInfo = [ordered]@{
    commit = $Commit
    build_date = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $Generated 'build_info.json'), $BuildInfo, $Utf8NoBom)

$Dist = [System.IO.Path]::GetFullPath((Join-Path $Root 'dist'))
$Staging = [System.IO.Path]::GetFullPath((Join-Path $Dist '.current-staging'))
$Current = [System.IO.Path]::GetFullPath((Join-Path $Dist 'CreatorAssistant-current'))
$CurrentExe = Join-Path $Current 'CreatorAssistant.exe'
if (-not $Staging.StartsWith($Dist, [System.StringComparison]::OrdinalIgnoreCase) -or -not $Current.StartsWith($Dist, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unsafe build output path.'
}
$Running = @(Get-Process CreatorAssistant -ErrorAction SilentlyContinue | Where-Object {
    try { [System.IO.Path]::GetFullPath($_.Path) -eq [System.IO.Path]::GetFullPath($CurrentExe) } catch { $false }
})
if ($Running.Count -gt 0) {
    throw "CreatorAssistant-current.exe сейчас запущен. Закройте текущую сборку и повторите build. PID: $($Running.Id -join ', ')"
}
$OldBuilds = @(Get-Process CreatorAssistant -ErrorAction SilentlyContinue | Where-Object {
    try { [System.IO.Path]::GetFullPath($_.Path) -ne [System.IO.Path]::GetFullPath($CurrentExe) } catch { $false }
})
if ($OldBuilds.Count -gt 0) {
    Write-Warning "Запущена старая сборка Creator Assistant. После сборки закройте её и запускайте приложение через обновлённый ярлык. PID: $($OldBuilds.Id -join ', ')"
}
if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }

Push-Location $Root
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $Staging `
        (Join-Path $Root 'CreatorAssistant.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
} finally {
    Pop-Location
}

$StagedApp = Join-Path $Staging 'CreatorAssistant'
$StagedExe = Join-Path $StagedApp 'CreatorAssistant.exe'
if (-not (Test-Path -LiteralPath $StagedExe)) { throw "EXE was not found after build: $StagedExe" }
if (Test-Path -LiteralPath $Current) { Remove-Item -LiteralPath $Current -Recurse -Force }
Move-Item -LiteralPath $StagedApp -Destination $Current
if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging }
$LaunchArguments = if ($VerifyUiReport) { '"--verify-template-ui=' + [System.IO.Path]::GetFullPath($VerifyUiReport) + '"' } else { '' }
& (Join-Path $PSScriptRoot 'update-desktop-shortcut.ps1') `
    -TargetPath $CurrentExe `
    -CommitHash $Commit `
    -Launch:($VerifyLaunch -or [bool]$VerifyUiReport) `
    -LaunchArguments $LaunchArguments
if ($LASTEXITCODE -ne 0) { throw 'Desktop shortcut update failed.' }
if ($VerifyUiReport) {
    $Report = [System.IO.Path]::GetFullPath($VerifyUiReport)
    for ($Attempt = 0; $Attempt -lt 60 -and -not (Test-Path -LiteralPath $Report); $Attempt++) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path -LiteralPath $Report)) { throw "Packaged UI verification report was not created: $Report" }
    $Verification = Get-Content -Raw -LiteralPath $Report | ConvertFrom-Json
    if (-not $Verification.success) { throw "Packaged UI verification failed: $Report" }
    Write-Host "Packaged UI verified: $Report"
}
Write-Host "Ready: $CurrentExe"
Write-Host "Commit: $Commit"
