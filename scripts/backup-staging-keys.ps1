[CmdletBinding()]
param(
    [string]$Source = "C:\tmp\creator-assistant-6a-keys",
    [string]$Output = "C:\tmp\creator-assistant-staging-keys-backup.dpapi.json",
    [string]$InventoryOutput = "C:\tmp\creator-assistant-key-inventory.json"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Key source directory not found: $Source"
}
$items = @()
$inventory = @()
foreach ($file in Get-ChildItem -LiteralPath $Source -File | Sort-Object Name) {
    $role = if ($file.Name -like "license-staging.*") { "staging-license-signing" }
        elseif ($file.Name -like "update-beta.*") { "staging-update-signing" }
        elseif ($file.Name -like "license-production.*") { "production-do-not-deploy-to-staging" }
        elseif ($file.Name -like "update-stable.*") { "production-do-not-deploy-to-staging" }
        elseif ($file.Name -like "update-developer.*") { "developer-do-not-deploy-to-staging" }
        else { "unclassified" }
    $plain = Get-Content -Raw -LiteralPath $file.FullName
    $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
    $items += [ordered]@{
        name = $file.Name
        role = $role
        protected_value = ConvertFrom-SecureString -SecureString $secure
    }
    $entry = [ordered]@{ name = $file.Name; role = $role; bytes = $file.Length }
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
Write-Host "Encrypted DPAPI backup created: $Output"
Write-Host "Secret-free inventory created: $InventoryOutput"
Write-Host "Source files were not deleted."
