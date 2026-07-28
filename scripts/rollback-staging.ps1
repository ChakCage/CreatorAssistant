[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SshHost,
    [Parameter(Mandatory)][string]$SshUser,
    [string]$SshKey,
    [string]$RemoteRoot = "/opt/creator-assistant-staging",
    [switch]$ConfirmRollback
)
$ErrorActionPreference = "Stop"
if (-not $ConfirmRollback) { throw "Rollback requires explicit -ConfirmRollback." }
$ssh = @()
if ($SshKey) { $ssh += @("-i", $SshKey) }
$remote = @"
set -e
previous=`$(find '$RemoteRoot/releases' -mindepth 1 -maxdepth 1 -type d | sort -r | sed -n '2p')
test -n "`$previous"
cp '$RemoteRoot/shared/.env.staging' "`$previous/deployment/staging/.env.staging"
chmod 600 "`$previous/deployment/staging/.env.staging"
cd "`$previous/deployment/staging"
docker compose --env-file .env.staging build
docker compose --env-file .env.staging up -d --remove-orphans
ln -sfn "`$previous" '$RemoteRoot/current'
"@
$remote = $remote.Replace("`r`n", "`n")
ssh @ssh "$SshUser@$SshHost" $remote
if ($LASTEXITCODE) { throw "Rollback failed." }
