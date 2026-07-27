[CmdletBinding()]
param([string]$Repository = "")
$ErrorActionPreference = "Stop"
if (-not $Repository) { $Repository = Split-Path -Parent $PSScriptRoot }
$tracked = git -C $Repository ls-files --cached --others --exclude-standard
if ($LASTEXITCODE -ne 0) { throw "Cannot enumerate tracked files." }
$patterns = @(
    '-----BEGIN (OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY-----',
    '(?im)^[ \t]*(bot_token|postgres_password|redis_password|backup_encryption_key)[ \t]*=[ \t]*[^$\r\n \t][^\r\n]{12,}',
    '(?im)^[ \t]*LICENSE_SIGNING_PRIVATE_KEY[ \t]*=[ \t]*[A-Za-z0-9_-]{32,}'
)
$violations = @()
foreach ($file in $tracked) {
    if ($file -eq "scripts/scan-staging-secrets.ps1") { continue }
    $path = Join-Path $Repository $file
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $text = Get-Content -Raw -LiteralPath $path -ErrorAction SilentlyContinue
    foreach ($pattern in $patterns) {
        if ($text -match $pattern) { $violations += $file; break }
    }
}
if ($violations.Count) {
    $violations | Sort-Object -Unique | ForEach-Object { Write-Error "Possible secret in tracked file: $_" }
    exit 1
}
Write-Host "Secret scan passed for $($tracked.Count) tracked files."
