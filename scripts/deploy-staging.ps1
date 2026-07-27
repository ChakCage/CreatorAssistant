[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SshHost,
    [Parameter(Mandatory)][string]$SshUser,
    [string]$SshKey,
    [string]$RemoteRoot = "/opt/creator-assistant-staging",
    [switch]$ConfirmDeploy,
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
if (-not $ConfirmDeploy) { throw "Deployment requires explicit -ConfirmDeploy." }
$repo = Split-Path -Parent $PSScriptRoot
if ((git -C $repo status --porcelain)) { throw "Refusing to deploy a dirty worktree." }
if (-not $SkipTests) {
    $testProfile = Join-Path $repo ".test-runtime\deploy-test-profile"
    New-Item -ItemType Directory -Force -Path $testProfile | Out-Null
    $env:LOCALAPPDATA = $testProfile
    $env:APPDATA = $testProfile
    $env:XDG_DATA_HOME = $testProfile
    Push-Location $repo
    try { & (Join-Path $repo ".venv\Scripts\python.exe") -m pytest -q }
    finally { Pop-Location }
    if ($LASTEXITCODE) { throw "Tests failed." }
    Push-Location (Join-Path $repo "services\licensing_backend")
    try { & (Join-Path $repo ".venv\Scripts\python.exe") -m pytest tests -q }
    finally { Pop-Location }
    if ($LASTEXITCODE) { throw "Backend tests failed." }
    Push-Location (Join-Path $repo "services\creator_assistant_bot")
    try { & (Join-Path $repo ".venv\Scripts\python.exe") -m pytest tests -q }
    finally { Pop-Location }
    if ($LASTEXITCODE) { throw "Bot tests failed." }
}
& (Join-Path $PSScriptRoot "scan-staging-secrets.ps1") -Repository $repo
if ($LASTEXITCODE) { throw "Secret scan failed." }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $env:TEMP "creator-assistant-staging-$stamp.tar"
try {
    git -C $repo archive --format=tar --output=$archive HEAD
    if ($LASTEXITCODE) { throw "git archive failed." }
    $ssh = @()
    if ($SshKey) { $ssh += @("-i", $SshKey) }
    ssh @ssh "$SshUser@$SshHost" "mkdir -p '$RemoteRoot/releases/$stamp' '$RemoteRoot/shared'"
    if ($LASTEXITCODE) { throw "Cannot prepare remote release." }
    scp @ssh $archive "$SshUser@${SshHost}:$RemoteRoot/releases/$stamp/source.tar"
    if ($LASTEXITCODE) { throw "Cannot upload release archive." }
    $remote = @"
set -e
test -f '$RemoteRoot/shared/.env.staging'
test "`$(stat -c '%a' '$RemoteRoot/shared/.env.staging')" = 600
cd '$RemoteRoot/releases/$stamp'
tar -xf source.tar
cp '$RemoteRoot/shared/.env.staging' deployment/staging/.env.staging
chmod 600 deployment/staging/.env.staging
cd deployment/staging
docker compose --env-file .env.staging config --quiet
docker compose --env-file .env.staging build
docker compose --env-file .env.staging run --rm migrate
docker compose --env-file .env.staging up -d --remove-orphans
ln -sfn '$RemoteRoot/releases/$stamp' '$RemoteRoot/current'
"@
    ssh @ssh "$SshUser@$SshHost" $remote
    if ($LASTEXITCODE) {
        try {
            & (Join-Path $PSScriptRoot "rollback-staging.ps1") -SshHost $SshHost -SshUser $SshUser -SshKey $SshKey -RemoteRoot $RemoteRoot -ConfirmRollback
        } catch {
            Write-Error "Deploy and automatic rollback both failed: $_"
        }
        throw "Staging deployment failed and rollback was requested."
    }
    & (Join-Path $PSScriptRoot "verify-staging.ps1") -SshHost $SshHost -SshUser $SshUser -SshKey $SshKey -RemoteRoot $RemoteRoot
} finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
