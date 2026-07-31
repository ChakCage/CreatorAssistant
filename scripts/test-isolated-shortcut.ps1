param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$ReportPath = ""
)

$ErrorActionPreference = 'Stop'
$Parent = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'CreatorAssistant-E2E'
$Root = Join-Path $Parent ('Shortcut-' + [Guid]::NewGuid().ToString('N'))
$Desktop = Join-Path $Root 'Desktop'
$Install = Join-Path $Root 'Install'
New-Item -ItemType Directory -Force -Path $Desktop,$Install | Out-Null
if (-not $ReportPath) { $ReportPath = Join-Path $Root 'shortcut-test.json' }
$PreviousDesktop = $env:CREATOR_ASSISTANT_E2E_DESKTOP
$env:CREATOR_ASSISTANT_E2E_DESKTOP = $Desktop
$Report = [ordered]@{ success=$false; isolated_desktop=$Desktop; install=$Install }
try {
    $Process = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR=' + $Install),'/MERGETASKS=desktopicon' -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) { throw "Installer exit code $($Process.ExitCode)" }
    $Shortcut = Get-ChildItem -LiteralPath $Desktop -Filter '*.lnk' | Select-Object -First 1
    if (-not $Shortcut) { throw 'Shortcut was not created in the isolated Desktop.' }
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut($Shortcut.FullName)
    $ExpectedExe = Join-Path $Install 'CreatorAssistant-Commercial-Staging.exe'
    $TargetOk = [IO.Path]::GetFullPath($Link.TargetPath) -eq [IO.Path]::GetFullPath($ExpectedExe)
    $WorkingOk = [IO.Path]::GetFullPath($Link.WorkingDirectory) -eq [IO.Path]::GetFullPath($Install)
    $NameOk = $Shortcut.BaseName -eq 'Creator Assistant Commercial Staging'
    $Uninstaller = Join-Path $Install 'unins000.exe'
    Start-Process -FilePath $Uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -WindowStyle Hidden
    $Removed = -not (Test-Path -LiteralPath $Shortcut.FullName)
    $Report.target=$Link.TargetPath; $Report.working_directory=$Link.WorkingDirectory
    $Report.name=$Shortcut.BaseName; $Report.removed_after_uninstall=$Removed
    $Report.success=$TargetOk -and $WorkingOk -and $NameOk -and $Removed
} finally {
    $env:CREATOR_ASSISTANT_E2E_DESKTOP = $PreviousDesktop
    $Report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
}
if (-not $Report.success) { throw "Isolated shortcut test failed. Report: $ReportPath" }
Write-Host "Shortcut test passed: $ReportPath"
