[CmdletBinding()]
param(
    [string]$Source = "C:\tmp\creator-assistant-6a-keys",
    [string]$Output = "C:\tmp\creator-assistant-staging-keys-backup.dpapi.json",
    [string]$InventoryOutput = "C:\tmp\creator-assistant-key-inventory.json",
    [switch]$SkipRestoreVerification
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Key source directory not found: $Source"
}
$allowedNames = @(
    "license-staging.private",
    "license-staging.public",
    "update-beta.private",
    "update-beta.public"
)
$sourceFiles = @($allowedNames | ForEach-Object {
    $path = Join-Path $Source $_
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required staging key file not found: $path"
    }
    Get-Item -LiteralPath $path
})
$items = @()
$inventory = @()
foreach ($file in $sourceFiles | Sort-Object Name) {
    $role = if ($file.Name -like "license-staging.*") { "staging-license-signing" }
        else { "staging-update-signing" }
    $plain = Get-Content -Raw -LiteralPath $file.FullName
    $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
    $items += [ordered]@{
        name = $file.Name
        role = $role
        protected_value = ConvertFrom-SecureString -SecureString $secure
    }
    $entry = [ordered]@{
        name = $file.Name
        role = $role
        key_id = [IO.Path]::GetFileNameWithoutExtension($file.Name)
        bytes = $file.Length
    }
    if ($file.Name.EndsWith(".public")) {
        $entry.fingerprint_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
    }
    $inventory += $entry
}
$backup = [ordered]@{
    format = "creator-assistant-dpapi-key-backup-v1"
    protection = "Windows DPAPI CurrentUser"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    source = $Source
    files = $items
}
$backup | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Output -Encoding UTF8
$inventory | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $InventoryOutput -Encoding UTF8

if (-not $SkipRestoreVerification) {
    $verificationRoot = Join-Path $env:TEMP ("creator-assistant-staging-key-restore-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $verificationRoot | Out-Null
    try {
        $restored = Get-Content -Raw -LiteralPath $Output | ConvertFrom-Json
        if ($restored.format -ne "creator-assistant-dpapi-key-backup-v1") {
            throw "Unexpected backup format."
        }
        foreach ($item in $restored.files) {
            if ($allowedNames -notcontains $item.name) {
                throw "Backup contains a non-staging key: $($item.name)"
            }
            $secure = ConvertTo-SecureString -String $item.protected_value
            $credential = [PSCredential]::new("restore-verification", $secure)
            $plain = $credential.GetNetworkCredential().Password
            $restoredPath = Join-Path $verificationRoot $item.name
            [IO.File]::WriteAllText($restoredPath, $plain, [Text.Encoding]::ASCII)
            $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Source $item.name)).Hash
            $restoredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $restoredPath).Hash
            if ($sourceHash -ne $restoredHash) {
                throw "DPAPI restore verification failed for $($item.name)"
            }
        }
        Write-Host "DPAPI restore verification passed for four staging key files."
    } finally {
        if (Test-Path -LiteralPath $verificationRoot) {
            foreach ($temporary in Get-ChildItem -LiteralPath $verificationRoot -File -ErrorAction SilentlyContinue) {
                $stream = [IO.File]::Open($temporary.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    $zeros = New-Object byte[] $stream.Length
                    $stream.Write($zeros, 0, $zeros.Length)
                    $stream.Flush($true)
                } finally {
                    $stream.Dispose()
                }
                Remove-Item -LiteralPath $temporary.FullName -Force
            }
            Remove-Item -LiteralPath $verificationRoot -Force
        }
    }
}
Write-Host "Encrypted DPAPI backup created: $Output"
Write-Host "Secret-free inventory created: $InventoryOutput"
Write-Host "Source files were not deleted."
