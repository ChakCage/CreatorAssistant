[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SshHost,
    [Parameter(Mandatory)][string]$SshUser,
    [string]$SshKey,
    [string]$RemoteRoot = "/opt/creator-assistant-staging",
    [string]$SecretsFile,
    [string]$LicensePrivateKeyFile,
    [string]$InstallerPath,
    [switch]$BootstrapDocker,
    [switch]$ConfirmDeploy,
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
if (-not $ConfirmDeploy) { throw "Deployment requires explicit -ConfirmDeploy." }
$repo = Split-Path -Parent $PSScriptRoot
if ((git -C $repo status --porcelain)) { throw "Refusing to deploy a dirty worktree." }
$releaseCommit = (git -C $repo rev-parse HEAD).Trim()
if ($releaseCommit -notmatch '^[0-9a-f]{40}$') { throw "Cannot resolve release commit." }
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
    $ssh = @("-o", "IdentitiesOnly=yes")
    if ($SshKey) { $ssh += @("-i", $SshKey) }
    $bootstrap = if ($BootstrapDocker) {
@"
if ! docker compose version >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=`$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu `$VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
"@
    } else { "docker compose version >/dev/null" }
$prepare = @"
set -e
$bootstrap
install -d -m 700 '$RemoteRoot' '$RemoteRoot/shared' '$RemoteRoot/shared/secrets'
install -d -m 755 '$RemoteRoot/releases' '$RemoteRoot/releases/$stamp' '$RemoteRoot/shared/releases'
"@
    $prepare = $prepare.Replace("`r`n", "`n")
    ssh @ssh "$SshUser@$SshHost" $prepare
    if ($LASTEXITCODE) { throw "Cannot prepare remote release." }
    if ($SecretsFile) {
        $resolvedSecrets = (Resolve-Path -LiteralPath $SecretsFile).Path
        if ($resolvedSecrets.StartsWith($repo, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Secrets file must be outside the Git repository."
        }
        scp @ssh $resolvedSecrets "$SshUser@${SshHost}:$RemoteRoot/shared/.env.staging.upload"
        if ($LASTEXITCODE) { throw "Cannot upload staging secrets." }
        ssh @ssh "$SshUser@$SshHost" "install -m 600 '$RemoteRoot/shared/.env.staging.upload' '$RemoteRoot/shared/.env.staging' && rm -f '$RemoteRoot/shared/.env.staging.upload'"
        if ($LASTEXITCODE) { throw "Cannot install staging secrets." }
    }
    if ($LicensePrivateKeyFile) {
        $resolvedKey = (Resolve-Path -LiteralPath $LicensePrivateKeyFile).Path
        scp @ssh $resolvedKey "$SshUser@${SshHost}:$RemoteRoot/shared/secrets/license-staging.private.upload"
        if ($LASTEXITCODE) { throw "Cannot upload staging license key." }
        ssh @ssh "$SshUser@$SshHost" "install -m 600 '$RemoteRoot/shared/secrets/license-staging.private.upload' '$RemoteRoot/shared/secrets/license-staging.private' && rm -f '$RemoteRoot/shared/secrets/license-staging.private.upload'"
        if ($LASTEXITCODE) { throw "Cannot install staging license key." }
    }
    if ($InstallerPath) {
        $resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path
        $installerName = Split-Path -Leaf $resolvedInstaller
        scp @ssh $resolvedInstaller "$SshUser@${SshHost}:$RemoteRoot/shared/releases/$installerName.upload"
        if ($LASTEXITCODE) { throw "Cannot upload staging installer." }
        ssh @ssh "$SshUser@$SshHost" "install -m 644 '$RemoteRoot/shared/releases/$installerName.upload' '$RemoteRoot/shared/releases/$installerName' && rm -f '$RemoteRoot/shared/releases/$installerName.upload'"
        if ($LASTEXITCODE) { throw "Cannot publish staging installer." }
    }
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
sed -i '/^CREATOR_RELEASE_COMMIT=/d' deployment/staging/.env.staging
printf '%s\n' 'CREATOR_RELEASE_COMMIT=$releaseCommit' >> deployment/staging/.env.staging
python3 deployment/staging/ops/webhook_readiness.py --env-file '$RemoteRoot/shared/.env.staging' --phase pre --repair --clear-stale-error
cd deployment/staging
docker compose --env-file .env.staging config --quiet
docker compose --env-file .env.staging build
docker compose --env-file .env.staging run --rm migrate
docker compose --env-file .env.staging up -d --remove-orphans
set -a; . ./.env.staging; set +a
for attempt in `$(seq 1 40); do
  bot_id=`$(docker compose --env-file .env.staging ps -q bot)
  bot_health=`$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "`$bot_id" 2>/dev/null || true)
  if [ "`$bot_health" = healthy ] && curl --fail --silent --show-error --max-time 5 "https://`$STAGING_BOT_DOMAIN/ready" >/dev/null; then
    break
  fi
  if [ "`$attempt" = 40 ]; then
    echo 'Bot did not become operationally ready after deploy.' >&2
    exit 1
  fi
  sleep 3
done
# Always write the runtime secret after the new bot is ready. This is an
# idempotent setWebhook call; queued updates are preserved and no delete occurs.
post_ready=false
for attempt in `$(seq 1 6); do
  if python3 ops/webhook_readiness.py --env-file .env.staging --phase post --repair --force-set --clear-stale-error --require-empty; then
    post_ready=true
    break
  fi
  sleep 5
done
test "`$post_ready" = true
ln -sfn '$RemoteRoot/releases/$stamp' '$RemoteRoot/current'
"@
    $remote = $remote.Replace("`r`n", "`n")
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
