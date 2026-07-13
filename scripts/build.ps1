param(
    [switch]$SkipInstall
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

Push-Location $Root
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        (Join-Path $Root 'CreatorAssistant.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
} finally {
    Pop-Location
}

$Exe = Join-Path $Root 'dist\CreatorAssistant\CreatorAssistant.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw "EXE was not found after build: $Exe" }
Write-Host "Ready: $Exe"
