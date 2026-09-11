param(
    [ValidateSet('developer', 'commercial')][string]$Edition = 'commercial',
    [string]$Executable
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = if ($Edition -eq 'developer') {
        Join-Path $Root 'dist\CreatorAssistant-Developer-Preview\CreatorAssistant-Developer-Preview.exe'
    } else {
        Join-Path $Root 'dist\CreatorAssistant\CreatorAssistant.exe'
    }
}
$ResolvedExecutable = (Resolve-Path -LiteralPath $Executable -ErrorAction Stop).Path
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutName = if ($Edition -eq 'developer') { 'Creator Assistant Developer Preview' } else { 'Creator Assistant' }
$ShortcutPath = Join-Path $Desktop ($ShortcutName + '.lnk')
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ResolvedExecutable
$Shortcut.WorkingDirectory = Split-Path -Parent $ResolvedExecutable
$Shortcut.IconLocation = "$ResolvedExecutable,0"
$Shortcut.Description = if ($Edition -eq 'developer') {
    'Creator Assistant Developer Edition',
    'Creator Assistant Developer'
} else {
    'Creator Assistant Commercial Edition'
}
$Shortcut.Save()
Write-Host "Shortcut created: $ShortcutPath"
