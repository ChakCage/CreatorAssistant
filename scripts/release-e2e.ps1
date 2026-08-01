param(
    [Parameter(Mandatory = $true)][string]$VersionN,
    [Parameter(Mandatory = $true)][string]$VersionNPlusOne,
    [string]$InstallersDirectory = "",
    [string]$LicensePrivateKey = "C:\tmp\creator-assistant-6a-keys\license-staging.private",
    [string]$UpdatePrivateKey = "C:\tmp\creator-assistant-6a-keys\update-beta.private",
    [switch]$StagingOnly,
    [switch]$CandidateOnly
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not $InstallersDirectory) { $InstallersDirectory = Join-Path $Root 'dist\installers' }
$E2EParent = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'CreatorAssistant-E2E'
$E2ERoot = Join-Path $E2EParent ("Release-" + [Guid]::NewGuid().ToString('N'))
$Roaming = Join-Path $E2ERoot 'Roaming'
$Local = Join-Path $E2ERoot 'Local'
$Temp = Join-Path $E2ERoot 'Temp'
$Reports = Join-Path $E2ERoot 'Reports'
New-Item -ItemType Directory -Force -Path $Roaming,$Local,$Temp,$Reports | Out-Null
if (-not ([System.IO.Path]::GetFullPath($E2ERoot).StartsWith(
    [System.IO.Path]::GetFullPath($E2EParent + '\'), [StringComparison]::OrdinalIgnoreCase))) {
    throw "Unsafe E2E root: $E2ERoot"
}

$StagingN = Join-Path $InstallersDirectory "CreatorAssistant-Commercial-Staging-Setup-$VersionN.exe"
$StagingNext = Join-Path $InstallersDirectory "CreatorAssistant-Commercial-Staging-Setup-$VersionNPlusOne.exe"
$Developer = Join-Path $InstallersDirectory "CreatorAssistant-Developer-Setup-$VersionNPlusOne.exe"
$Commercial = Join-Path $InstallersDirectory "CreatorAssistant-Commercial-Setup-$VersionNPlusOne.exe"
$RequiredArtifacts = @($StagingNext,$LicensePrivateKey)
if (-not $CandidateOnly) { $RequiredArtifacts += @($StagingN,$UpdatePrivateKey) }
if (-not $StagingOnly) { $RequiredArtifacts += @($Developer,$Commercial) }
foreach ($Path in $RequiredArtifacts) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Required E2E artifact is missing: $Path" }
}

$Previous = @{}
foreach ($Name in @('APPDATA','LOCALAPPDATA','TEMP','TMP','PYTHONPATH','LICENSE_ENV','LICENSE_DATABASE_URL',
    'LICENSE_ACTIVATION_PEPPER','LICENSE_SIGNING_KEY_ID','LICENSE_SIGNING_PRIVATE_KEY',
    'LICENSE_ADMIN_TOKEN_HASH','LICENSE_PUBLIC_BASE_URL','CREATOR_ASSISTANT_E2E',
    'CREATOR_ASSISTANT_E2E_LICENSE_URL','CREATOR_ASSISTANT_E2E_UPDATE_URL',
    'CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE','CREATOR_ASSISTANT_E2E_PRESERVE_LICENSE',
    'CREATOR_ASSISTANT_E2E_INSTALL_UPDATE')) {
    $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}

$Backend = $null
$Files = $null
$Report = [ordered]@{
    schema_version = 1
    root = $E2ERoot
    version_n = $VersionN
    version_n_plus_one = $VersionNPlusOne
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    success = $false
    steps = @()
}

function Add-Step([string]$Name, [bool]$Success, [string]$Details = '') {
    $Report.steps += [ordered]@{ name=$Name; success=$Success; details=$Details; at=(Get-Date).ToUniversalTime().ToString('o') }
    Write-Host ("[{0}] {1}: {2}" -f $(if ($Success) { 'OK' } else { 'FAIL' }), $Name, $Details)
    if (-not $Success) { throw "$Name failed: $Details" }
}

function Invoke-Installer([string]$Path, [string]$TargetDirectory) {
    $Process = Start-Process -FilePath $Path -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
        '/MERGETASKS=!desktopicon',("/DIR=" + $TargetDirectory) -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) { throw "Installer exit code $($Process.ExitCode): $Path" }
}

function Wait-File([string]$Path, [int]$Seconds = 90) {
    $Limit = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $Limit) {
        if (Test-Path -LiteralPath $Path) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out waiting for $Path"
}

try {
    $env:APPDATA = $Roaming; $env:LOCALAPPDATA = $Local; $env:TEMP = $Temp; $env:TMP = $Temp
    $env:PYTHONPATH = Join-Path $Root 'services\licensing_backend'
    $env:LICENSE_ENV = 'local'
    $env:LICENSE_DATABASE_URL = 'sqlite+pysqlite:///' + ((Join-Path $E2ERoot 'licensing.db') -replace '\\','/')
    $env:LICENSE_ACTIVATION_PEPPER = 'creator-assistant-e2e-pepper-not-production'
    $env:LICENSE_SIGNING_KEY_ID = 'staging-license-v1'
    $env:LICENSE_SIGNING_PRIVATE_KEY = (Get-Content -Raw -LiteralPath $LicensePrivateKey).Trim()
    $AdminToken = 'creator-assistant-e2e-admin'
    $env:LICENSE_ADMIN_TOKEN_HASH = (& $Python -c "import hashlib;print(hashlib.sha256(b'$AdminToken').hexdigest())").Trim()
    $env:LICENSE_PUBLIC_BASE_URL = 'http://127.0.0.1:8765'
    $env:CREATOR_ASSISTANT_E2E = '1'
    $env:CREATOR_ASSISTANT_E2E_LICENSE_URL = 'http://127.0.0.1:8765'
    $env:CREATOR_ASSISTANT_E2E_UPDATE_URL = 'http://127.0.0.1:8765'
    $env:CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE = 'CreatorAssistant/E2E/' + [Guid]::NewGuid().ToString('N')
    $env:CREATOR_ASSISTANT_E2E_PRESERVE_LICENSE = '1'

    Push-Location (Join-Path $Root 'services\licensing_backend')
    try {
        & $Python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Local database migration failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }

    $Backend = Start-Process -FilePath $Python -ArgumentList '-m','uvicorn','app.api:app','--host','127.0.0.1','--port','8765' `
        -WorkingDirectory (Join-Path $Root 'services\licensing_backend') -PassThru -WindowStyle Hidden
    $Files = Start-Process -FilePath $Python -ArgumentList '-m','http.server','8766','--bind','127.0.0.1','--directory',$InstallersDirectory `
        -PassThru -WindowStyle Hidden
    $Ready = $false
    for ($i=0; $i -lt 60 -and -not $Ready; $i++) {
        $ReadyBody = & curl.exe --silent --show-error --max-time 2 'http://127.0.0.1:8765/ready' 2>$null
        $Ready = $LASTEXITCODE -eq 0 -and ($ReadyBody -match '"status"\s*:\s*"ready"')
        if (-not $Ready) { Start-Sleep -Milliseconds 500 }
    }
    Add-Step 'local-backend-ready' $Ready 'localhost only'

    $InstalledStaging = Join-Path $Local 'Programs\CreatorAssistant\Commercial-Staging\CreatorAssistant-Commercial-Staging.exe'
    $InitialInstaller = if ($CandidateOnly) { $StagingNext } else { $StagingN }
    Invoke-Installer $InitialInstaller (Split-Path $InstalledStaging)
    Wait-File $InstalledStaging
    Add-Step 'install-staging-n' $true $InstalledStaging

    $Profile = Join-Path $Roaming 'CreatorAssistant\CommercialStaging'
    $Brand = Join-Path $Local 'CreatorAssistant\Shared\BrandAssets\e2e'
    $CyrillicName = ([string][char]0x0422) + [char]0x0435 + [char]0x0441 + [char]0x0442
    $Project = Join-Path $E2ERoot ('Projects\Cyrillic ' + $CyrillicName)
    New-Item -ItemType Directory -Force -Path $Profile,$Brand,$Project | Out-Null
    $Utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $Profile 'settings.json'), '{"_schema_version":2,"youtube_root":"E:/E2E-preserve-me","commercial_setup":{"completed":true}}', $Utf8)
    [IO.File]::WriteAllText((Join-Path $Brand 'banner.txt'), 'brand-marker', $Utf8)
    [IO.File]::WriteAllText((Join-Path $Project 'project.txt'), 'project-marker', $Utf8)

    $Headers = @{'X-Admin-Token'=$AdminToken}
    $User = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/v1/admin/users' -Headers $Headers -ContentType 'application/json' -Body '{"email":"e2e@example.invalid"}'
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/v1/admin/subscriptions/grant' -Headers $Headers -ContentType 'application/json' `
        -Body (@{user_id=$User.id;plan_code='beta';days=30;reason='isolated release e2e'} | ConvertTo-Json) | Out-Null
    $Code = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/v1/admin/activation-codes' -Headers $Headers -ContentType 'application/json' `
        -Body (@{user_id=$User.id;ttl_minutes=30;reason='isolated release e2e'} | ConvertTo-Json)
    $env:CREATOR_ASSISTANT_E2E_ACTIVATION_CODE = $Code.activation_code
    $LicenseReport = Join-Path $Reports 'license.json'
    Start-Process -FilePath $InstalledStaging -ArgumentList "--verify-commercial-license=$LicenseReport" -Wait -WindowStyle Hidden
    Wait-File $LicenseReport
    $LicenseValue = Get-Content -Raw -LiteralPath $LicenseReport | ConvertFrom-Json
    Add-Step 'activate-and-restart-license' ([bool]$LicenseValue.success) $LicenseReport
    Add-Step 'offline-grace-before-boundary' ($LicenseValue.offline_before_boundary -eq 'OFFLINE_GRACE') $LicenseValue.offline_grace_boundary
    Add-Step 'offline-grace-after-boundary-blocked' ([bool]$LicenseValue.feature_gate_blocked_after_boundary) $LicenseValue.offline_after_boundary
    Add-Step 'offline-grace-online-recovery' ($LicenseValue.online_recovery -eq 'ACTIVE') $LicenseValue.online_recovery

    $ProjectFixtureReport = Join-Path $Reports 'packaged-project-fixture.json'
    Start-Process -FilePath $InstalledStaging -ArgumentList "--verify-project-fixture=$ProjectFixtureReport" -Wait -WindowStyle Hidden
    Wait-File $ProjectFixtureReport
    $ProjectFixtureValue = Get-Content -Raw -LiteralPath $ProjectFixtureReport | ConvertFrom-Json
    Add-Step 'short-packaged-project-fixture' ([bool]$ProjectFixtureValue.success) $ProjectFixtureReport

    if (-not $CandidateOnly) {
        $Hash = (Get-FileHash -LiteralPath $StagingNext -Algorithm SHA256).Hash.ToLowerInvariant()
        $Size = (Get-Item -LiteralPath $StagingNext).Length
        $Create = & $Python -m app.admin_cli create-release --edition commercial --channel beta --version $VersionNPlusOne `
            --build-number 900002 --architecture x86_64 --download-url "http://127.0.0.1:8766/$(Split-Path -Leaf $StagingNext)" `
            --sha256 $Hash --file-size $Size --minimum-supported-version $VersionN --notes 'Local signed update E2E'
        $ReleaseId = ($Create | ConvertFrom-Json).id
        $Manifest = Join-Path $Reports 'release-manifest.json'
        & $Python -m app.admin_cli sign-release-manifest --release $ReleaseId --private-key-file $UpdatePrivateKey --key-id staging-update-local --output $Manifest | Out-Null
        & $Python -m app.admin_cli activate-release --release $ReleaseId | Out-Null
        Add-Step 'signed-release-created' (Test-Path -LiteralPath $Manifest) $Manifest

        $UpdateReport = Join-Path $Reports 'update.json'
        $env:CREATOR_ASSISTANT_E2E_INSTALL_UPDATE = '1'
        Start-Process -FilePath $InstalledStaging -ArgumentList "--verify-update=$UpdateReport" -Wait -WindowStyle Hidden
        Wait-File $UpdateReport
        $UpdateValue = Get-Content -Raw -LiteralPath $UpdateReport | ConvertFrom-Json
        Add-Step 'application-update-check-download-launch' ([bool]$UpdateValue.success) $UpdateReport
        Start-Sleep -Seconds 8
        Wait-File $InstalledStaging

        $PostUpdate = Join-Path $Reports 'post-update.json'
        Start-Process -FilePath $InstalledStaging -ArgumentList "--verify-edition=$PostUpdate" -Wait -WindowStyle Hidden
        Wait-File $PostUpdate
        $PostValue = Get-Content -Raw -LiteralPath $PostUpdate | ConvertFrom-Json
        Add-Step 'updated-version-starts' ($PostValue.build.version -eq $VersionNPlusOne) $PostValue.build.version
    }
    $Preserved = (Test-Path (Join-Path $Profile 'settings.json')) -and (Test-Path (Join-Path $Brand 'banner.txt')) -and (Test-Path (Join-Path $Project 'project.txt'))
    Add-Step 'update-preserves-settings-license-project-brand-model-roots' $Preserved 'all markers exist'

    $Uninstaller = Join-Path (Split-Path $InstalledStaging) 'unins000.exe'
    Start-Process -FilePath $Uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -WindowStyle Hidden
    $PreservedAfterUninstall = (-not (Test-Path $InstalledStaging)) -and (Test-Path (Join-Path $Profile 'settings.json')) -and (Test-Path (Join-Path $Brand 'banner.txt')) -and (Test-Path (Join-Path $Project 'project.txt'))
    Add-Step 'uninstall-preserves-user-data-by-default' $PreservedAfterUninstall 'program removed, markers retained'

    Invoke-Installer $StagingNext (Split-Path $InstalledStaging)
    Wait-File $InstalledStaging
    Add-Step 'reinstall-recovers-data' ((Get-Content -Raw (Join-Path $Profile 'settings.json')) -match 'E:/E2E-preserve-me') 'settings marker restored'

    if (-not $StagingOnly) {
        $DeveloperExe = Join-Path $Local 'Programs\CreatorAssistant\Developer\CreatorAssistant-Developer.exe'
        $CommercialExe = Join-Path $Local 'Programs\CreatorAssistant\Commercial\CreatorAssistant.exe'
        Invoke-Installer $Developer (Split-Path $DeveloperExe)
        Invoke-Installer $Commercial (Split-Path $CommercialExe)
        Add-Step 'parallel-three-editions' ((Test-Path $InstalledStaging) -and (Test-Path $DeveloperExe) -and (Test-Path $CommercialExe)) 'three independent install roots'
    }

    $Report.success = ($Report.steps | Where-Object { -not $_.success }).Count -eq 0
}
finally {
    foreach ($Process in ($Backend,$Files)) {
        if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue }
    }
    if ($env:CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE) {
        try {
            foreach ($CredentialSuffix in @(
                'installation/id',
                'license-entitlement/token',
                'license-refresh/credential',
                'license-state/metadata',
                'license/entitlement'
            )) {
                $CredentialTarget = $env:CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE.TrimEnd('/') + '/' + $CredentialSuffix
                & cmdkey.exe ("/delete:" + $CredentialTarget) 2>$null | Out-Null
            }
        } catch {
            $Report.steps += [ordered]@{ name='cleanup-test-credentials'; success=$false; details=$_.Exception.Message; at=(Get-Date).ToUniversalTime().ToString('o') }
        }
    }
    $Report.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $ReportPath = Join-Path $Root 'dist\installers\local-release-e2e.json'
    $Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    foreach ($Name in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process') }
    Write-Host "E2E report: $ReportPath"
}
