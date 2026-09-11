param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$Root = ""
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CommonGit = (& git -C $ProjectRoot rev-parse --path-format=absolute --git-common-dir).Trim()
$WorkspaceRoot = if ($LASTEXITCODE -eq 0 -and $CommonGit) { Split-Path $CommonGit } else { $ProjectRoot }
if (-not $Root) {
    $Root = Join-Path $WorkspaceRoot ('.test-runtime\dp-e2e-' + [Guid]::NewGuid().ToString('N'))
}
$Root = [IO.Path]::GetFullPath($Root)
if (-not $Root.StartsWith([IO.Path]::GetFullPath($WorkspaceRoot + '\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw "E2E root must be inside the workspace: $Root"
}
$Installer = (Resolve-Path -LiteralPath $Installer -ErrorAction Stop).Path
$Roaming = Join-Path $Root 'Roaming'
$Local = Join-Path $Root 'Local'
$Temp = Join-Path $Root 'Temp'
$Desktop = Join-Path $Root 'Desktop'
$Reports = Join-Path $Root 'Reports'
$InstallDir = Join-Path $Local 'Programs\CreatorAssistant\DeveloperPreview'
$Executable = Join-Path $InstallDir 'CreatorAssistant-Developer-Preview.exe'
$Shortcut = Join-Path $Desktop 'Creator Assistant Developer Preview.lnk'
$Profile = Join-Path $Roaming 'CreatorAssistant\DeveloperPreview'
New-Item -ItemType Directory -Force -Path $Roaming,$Local,$Temp,$Desktop,$Reports | Out-Null

$Previous = @{}
foreach ($Name in @('APPDATA','LOCALAPPDATA','TEMP','TMP','USERPROFILE','CREATOR_ASSISTANT_EDITION',
    'CREATOR_ASSISTANT_LICENSE_URL','CREATOR_ASSISTANT_UPDATE_URL','CREATOR_ASSISTANT_E2E_DESKTOP',
    'HTTP_PROXY','HTTPS_PROXY')) {
    $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}
$Report = [ordered]@{ schema_version=1; success=$false; root=$Root; installer=$Installer; steps=@() }
function Add-Step([string]$Name, [bool]$Success, [string]$Details='') {
    $Report.steps += [ordered]@{name=$Name;success=$Success;details=$Details;at=(Get-Date).ToUniversalTime().ToString('o')}
    Write-Host ("[{0}] {1}: {2}" -f $(if ($Success) {'OK'} else {'FAIL'}),$Name,$Details)
    if (-not $Success) { throw "$Name failed: $Details" }
}
function Invoke-App([string]$Argument, [string]$ExpectedReport) {
    $Process = Start-Process -FilePath $Executable -ArgumentList $Argument -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) { throw "Application exit code $($Process.ExitCode): $Argument" }
    if (-not (Test-Path -LiteralPath $ExpectedReport)) { throw "Application report missing: $ExpectedReport" }
    return Get-Content -Raw -LiteralPath $ExpectedReport | ConvertFrom-Json
}
try {
    $env:APPDATA=$Roaming; $env:LOCALAPPDATA=$Local; $env:TEMP=$Temp; $env:TMP=$Temp
    $env:USERPROFILE=$Root; $env:CREATOR_ASSISTANT_EDITION='developer'
    $env:CREATOR_ASSISTANT_LICENSE_URL=''; $env:CREATOR_ASSISTANT_UPDATE_URL=''
    $env:CREATOR_ASSISTANT_E2E_DESKTOP=$Desktop
    $env:HTTP_PROXY='http://127.0.0.1:9'; $env:HTTPS_PROXY='http://127.0.0.1:9'

    $Install = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
        '/MERGETASKS=desktopicon',('/DIR="' + $InstallDir + '"') -Wait -PassThru -WindowStyle Hidden
    Add-Step 'clean-install' ($Install.ExitCode -eq 0 -and (Test-Path -LiteralPath $Executable)) $Executable
    Add-Step 'desktop-shortcut' (Test-Path -LiteralPath $Shortcut) $Shortcut

    $EditionPath = Join-Path $Reports 'edition.json'
    $Edition = Invoke-App "--verify-edition=$EditionPath" $EditionPath
    Add-Step 'standalone-edition' ([bool]$Edition.success -and $Edition.edition -eq 'developer') ($Edition | ConvertTo-Json -Compress)
    Add-Step 'developer-features' (($Edition.tabs -contains 'Подготовка проекта') -and ($Edition.tabs -contains 'Shorts') -and
        ($Edition.tabs -contains 'Автопилот') -and ($Edition.tabs -contains 'Очередь публикаций')) ($Edition.tabs -join ', ')

    $SetupPath = Join-Path $Reports 'setup.json'
    $Setup = Invoke-App "--verify-developer-setup=$SetupPath" $SetupPath
    Add-Step 'first-run-five-pages' ([bool]$Setup.success -and $Setup.pages.Count -eq 5) $Setup.screenshot
    Add-Step 'fixed-local-model' ($Setup.model -eq 'qwen3.6:35b-a3b') $Setup.model
    Add-Step 'no-commercial-boundary' (-not $Setup.licensing_initialized -and $Setup.forbidden_modules_loaded.Count -eq 0) 'licensing and Commercial setup absent'

    $Settings = Join-Path $Profile 'settings.json'
    Add-Step 'isolated-profile-created' (Test-Path -LiteralPath $Settings) $Settings
    $Value = Get-Content -Raw -LiteralPath $Settings | ConvertFrom-Json
    $Value.developer_setup.ai_skipped = $true
    [IO.File]::WriteAllText($Settings, ($Value | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
    $RestartPath = Join-Path $Reports 'restart.json'
    $Restart = Invoke-App "--verify-edition=$RestartPath" $RestartPath
    $Reloaded = Get-Content -Raw -LiteralPath $Settings | ConvertFrom-Json
    Add-Step 'restart-preserves-settings' ([bool]$Restart.success -and [bool]$Reloaded.developer_setup.ai_skipped) 'developer_setup.ai_skipped=true'

    $Reinstall = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
        '/MERGETASKS=desktopicon',('/DIR="' + $InstallDir + '"') -Wait -PassThru -WindowStyle Hidden
    $Reloaded = Get-Content -Raw -LiteralPath $Settings | ConvertFrom-Json
    Add-Step 'reinstall-preserves-settings' ($Reinstall.ExitCode -eq 0 -and [bool]$Reloaded.developer_setup.ai_skipped) 'developer_setup.ai_skipped=true'

    $Uninstaller = Join-Path $InstallDir 'unins000.exe'
    $Uninstall = Start-Process -FilePath $Uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru -WindowStyle Hidden
    Add-Step 'uninstall-removes-program' ($Uninstall.ExitCode -eq 0 -and -not (Test-Path -LiteralPath $Executable)) $InstallDir
    Add-Step 'uninstall-preserves-user-data' (Test-Path -LiteralPath $Settings) $Settings
    Add-Step 'uninstall-removes-shortcut' (-not (Test-Path -LiteralPath $Shortcut)) $Shortcut
    $Report.success = $true
} finally {
    foreach ($Name in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Name,$Previous[$Name],'Process') }
    $Report.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $ReportPath = Join-Path $Reports 'developer-preview-e2e.json'
    $Report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Write-Host "E2E report: $ReportPath"
}
