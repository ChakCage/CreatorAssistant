[CmdletBinding()]
param(
    [string]$OutputDirectory = "C:\tmp\CreatorAssistant-Staging",
    [string]$ProtectedBackup = "$env:LOCALAPPDATA\CreatorAssistant\StagingSecrets\secrets.dpapi.json",
    [string]$RemoteRoot = "/opt/creator-assistant-staging"
)
$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$ReleaseCommit = (& git -C $Repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $ReleaseCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Unable to determine the release commit."
}

function New-RandomSecret([int]$Bytes = 48) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($buffer).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Protect-File([string]$Path) {
    & icacls.exe $Path /inheritance:r /grant:r "$env:USERNAME`:F" "SYSTEM`:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to protect local secret file: $Path" }
}

function Protect-Value([string]$Value) {
    $secure = ConvertTo-SecureString -String $Value -AsPlainText -Force
    return ConvertFrom-SecureString -SecureString $secure
}

$telegramSecure = Read-Host "Telegram bot token для @KazakovCreatorAssistantBot" -AsSecureString
$telegramToken = [PSCredential]::new("telegram", $telegramSecure).GetNetworkCredential().Password
if ($telegramToken -notmatch "^[0-9]+:[A-Za-z0-9_-]{30,}$") {
    throw "Telegram token has an unexpected format."
}

$secrets = [ordered]@{
    POSTGRES_PASSWORD = New-RandomSecret
    REDIS_PASSWORD = New-RandomSecret
    LICENSE_ACTIVATION_PEPPER = New-RandomSecret
    LICENSE_BOT_SERVICE_SECRET = New-RandomSecret
    CREATOR_BOT_TOKEN = $telegramToken
    CREATOR_BOT_WEBHOOK_SECRET = New-RandomSecret
    BACKUP_ENCRYPTION_KEY = New-RandomSecret 32
    LICENSE_CLI_ADMIN_TOKEN = New-RandomSecret
}
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $adminBytes = [Text.Encoding]::UTF8.GetBytes($secrets.LICENSE_CLI_ADMIN_TOKEN)
    $adminHash = (($sha.ComputeHash($adminBytes) | ForEach-Object { $_.ToString("x2") }) -join "")
} finally {
    $sha.Dispose()
}

New-Item -ItemType Directory -Force -Path $OutputDirectory,(Split-Path -Parent $ProtectedBackup) | Out-Null
$serverEnv = Join-Path $OutputDirectory "server.env.upload"
$adminEnv = Join-Path $OutputDirectory "admin.env.upload"
$lines = @(
    "COMPOSE_PROJECT_NAME=creator-assistant-staging"
    "STAGING_API_DOMAIN=api-staging.kazakovapps.ru"
    "STAGING_BOT_DOMAIN=bot-staging.kazakovapps.ru"
    "STAGING_DOWNLOAD_DOMAIN=download-staging.kazakovapps.ru"
    "TLS_EMAIL=chak74wow@gmail.com"
    "POSTGRES_DB=creator_assistant_staging"
    "POSTGRES_USER=creator_assistant"
    "POSTGRES_PASSWORD=$($secrets.POSTGRES_PASSWORD)"
    "REDIS_PASSWORD=$($secrets.REDIS_PASSWORD)"
    "LICENSE_ACTIVATION_PEPPER=$($secrets.LICENSE_ACTIVATION_PEPPER)"
    "LICENSE_SIGNING_KEY_ID=staging-license-v1"
    "STAGING_LICENSE_PRIVATE_KEY_FILE=$RemoteRoot/shared/secrets/license-staging.private"
    "LICENSE_ADMIN_TOKEN_HASH=$adminHash"
    "LICENSE_BOT_SERVICE_SECRET=$($secrets.LICENSE_BOT_SERVICE_SECRET)"
    "LICENSE_MINIMUM_APP_VERSION=0.3.0"
    "LICENSE_RECOMMENDED_APP_VERSION=0.3.1-beta.4"
    "CREATOR_RELEASE_VERSION=0.3.1-beta.4"
    "CREATOR_RELEASE_COMMIT=$ReleaseCommit"
    "CREATOR_BOT_TOKEN=$($secrets.CREATOR_BOT_TOKEN)"
    "CREATOR_BOT_WEBHOOK_SECRET=$($secrets.CREATOR_BOT_WEBHOOK_SECRET)"
    "CREATOR_BOT_SUPPORT_URL=https://t.me/KazakovCreatorAssistantBot?start=support"
    "CREATOR_BOT_HELP_URL=https://t.me/KazakovCreatorAssistantBot?start=help"
    "CREATOR_BOT_ADMIN_TELEGRAM_ID=424403653"
    "BACKUP_ENCRYPTION_KEY=$($secrets.BACKUP_ENCRYPTION_KEY)"
    "BACKUP_RETENTION_DAYS=7"
    "ALERT_WEBHOOK_URL="
    "STAGING_RELEASES_DIRECTORY=$RemoteRoot/shared/releases"
)
[IO.File]::WriteAllText($serverEnv, (($lines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText($adminEnv, "LICENSE_CLI_ADMIN_TOKEN=$($secrets.LICENSE_CLI_ADMIN_TOKEN)`n", [Text.UTF8Encoding]::new($false))
Protect-File $serverEnv
Protect-File $adminEnv

$protected = [ordered]@{
    format = "creator-assistant-staging-secrets-dpapi-v1"
    protection = "Windows DPAPI CurrentUser"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    values = [ordered]@{}
}
foreach ($entry in $secrets.GetEnumerator()) {
    $protected.values[$entry.Key] = Protect-Value $entry.Value
}
$protected | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ProtectedBackup -Encoding UTF8
Protect-File $ProtectedBackup

$telegramToken = $null
$telegramSecure.Dispose()
$secrets.CREATOR_BOT_TOKEN = $null
[GC]::Collect()
Write-Host "Protected staging secrets prepared."
Write-Host "DPAPI backup: $ProtectedBackup"
Write-Host "Upload files are ACL-restricted and contain no private signing key."
