param(
    [Parameter(Mandatory = $true)][ValidateSet('developer', 'commercial')][string]$Edition,
    [Parameter(Mandatory = $true)][string]$Executable,
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
$ResolvedExecutable = (Resolve-Path -LiteralPath $Executable -ErrorAction Stop).Path
$Archive = & $Python -m PyInstaller.utils.cliutils.archive_viewer -l -r $ResolvedExecutable 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "Unable to inspect PyInstaller archive: $ResolvedExecutable" }

$DeveloperOnly = @(
    'creator_assistant.ui.autopilot_tab',
    'creator_assistant.ui.publishing_queue',
    'creator_assistant.ui.publishing_accounts',
    'creator_assistant.services.automation',
    'creator_assistant.services.publishing',
    'creator_assistant.infrastructure.automation_job_store',
    'creator_assistant.infrastructure.publishing_store',
    'creator_assistant.infrastructure.credential_store',
    'creator_assistant.infrastructure.publishing_startup'
)

if ($Edition -eq 'commercial') {
    $Found = @($DeveloperOnly | Where-Object { $Archive.Contains($_) })
    if ($Found.Count -gt 0) {
        throw "Commercial package contains Developer-only modules: $($Found -join ', ')"
    }
} else {
    $Required = @(
        'creator_assistant.ui.autopilot_tab',
        'creator_assistant.ui.publishing_queue',
        'creator_assistant.services.automation',
        'creator_assistant.services.publishing'
    )
    $Missing = @($Required | Where-Object { -not $Archive.Contains($_) })
    if ($Missing.Count -gt 0) {
        throw "Developer package is missing required modules: $($Missing -join ', ')"
    }
}
Write-Host "Package boundary verified [$Edition]: $ResolvedExecutable"
