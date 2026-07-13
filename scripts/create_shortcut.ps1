param(
    [string]$Executable
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = Join-Path $Root 'dist\CreatorAssistant\CreatorAssistant.exe'
}
$ResolvedExecutable = (Resolve-Path -LiteralPath $Executable -ErrorAction Stop).Path
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Creator Assistant.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ResolvedExecutable
$Shortcut.WorkingDirectory = Split-Path -Parent $ResolvedExecutable
$Shortcut.IconLocation = "$ResolvedExecutable,0"
$Shortcut.Description = 'YouTube voice-over project preparation'
$Shortcut.Save()
Write-Host "Shortcut created: $ShortcutPath"
