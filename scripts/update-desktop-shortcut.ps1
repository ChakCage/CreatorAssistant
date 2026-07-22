param(
    [Parameter(Mandatory = $true)][string]$TargetPath,
    [string]$CommitHash = "",
    [string]$ShortcutName = "Creator Assistant",
    [switch]$Launch,
    [string]$LaunchArguments = ""
)

$ErrorActionPreference = 'Stop'
$TargetPath = [System.IO.Path]::GetFullPath($TargetPath)
if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
    throw "Creator Assistant EXE was not found: $TargetPath"
}
$WorkingDirectory = Split-Path -Parent $TargetPath
$Desktop = [Environment]::GetFolderPath('Desktop')
if (-not $Desktop) { throw 'Windows Desktop folder was not found.' }
$Desktop = [System.IO.Path]::GetFullPath($Desktop)
$ShortcutPath = Join-Path $Desktop ($ShortcutName + '.lnk')

$CandidateFolders = @($Desktop)
if ($env:USERPROFILE) { $CandidateFolders += (Join-Path $env:USERPROFILE 'Desktop') }
if ($env:OneDrive) { $CandidateFolders += (Join-Path $env:OneDrive 'Desktop') }
$CandidateFolders = $CandidateFolders | Select-Object -Unique

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = $WorkingDirectory
$Shortcut.IconLocation = "$TargetPath,0"
$Shortcut.Description = "Creator Assistant · commit $CommitHash"
$Shortcut.Save()

foreach ($Folder in $CandidateFolders) {
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { continue }
    $Duplicate = Join-Path $Folder ($ShortcutName + '.lnk')
    if ([System.IO.Path]::GetFullPath($Duplicate) -ne [System.IO.Path]::GetFullPath($ShortcutPath) -and (Test-Path -LiteralPath $Duplicate)) {
        Remove-Item -LiteralPath $Duplicate
    }
}

Write-Host "Shortcut: $ShortcutPath"
Write-Host "Target: $TargetPath"
Write-Host "Working directory: $WorkingDirectory"
if ($Launch) {
    $Command = '"' + $ShortcutPath + '"'
    if ($LaunchArguments) { $Command += ' ' + $LaunchArguments }
    $ExitCode = $Shell.Run($Command, 1, $false)
    if ($ExitCode -ne 0) { throw "Desktop shortcut launch failed: $ExitCode" }
    Write-Host "Launched through shortcut: $ShortcutPath"
}
