[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SshHost,
    [Parameter(Mandatory)][string]$SshUser,
    [string]$SshKey,
    [string]$RemoteRoot = "/opt/creator-assistant-staging"
)
$ErrorActionPreference = "Stop"
$ssh = @()
if ($SshKey) { $ssh += @("-i", $SshKey) }
$remote = @"
set -e
cd '$RemoteRoot/current/deployment/staging'
set -a; . ./.env.staging; set +a
docker compose --env-file .env.staging ps --status running
curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://`$STAGING_API_DOMAIN/ready"
curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://`$STAGING_BOT_DOMAIN/ready"
curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://`$STAGING_API_DOMAIN/v1/admin/events" --output /dev/null --write-out '%{http_code}' | grep -q 404
echo | openssl s_client -verify_return_error -verify_hostname "`$STAGING_API_DOMAIN" -servername "`$STAGING_API_DOMAIN" -connect "`$STAGING_API_DOMAIN:443" 2>/dev/null >/dev/null
"@
$remote = $remote.Replace("`r`n", "`n")
ssh @ssh "$SshUser@$SshHost" $remote
if ($LASTEXITCODE) { throw "Staging verification failed." }
Write-Host "Staging HTTPS, hostname verification, public health, and admin isolation passed."
