[CmdletBinding()]
param(
    [string]$KeyPath = "C:\Users\wachi\.ssh\creator_assistant_staging_v2",
    [string]$StatePath = "C:\tmp\creator-assistant-staging-ssh-agent.json"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH private key not found: $KeyPath"
}

$service = Get-Service -Name ssh-agent -ErrorAction SilentlyContinue
if (-not $service) {
    throw "Windows OpenSSH Authentication Agent service is not installed."
}
if ($service.Status -ne "Running") {
    throw "Windows OpenSSH Authentication Agent is not running. Start the ssh-agent service and retry."
}

Write-Host "Введите passphrase SSH-ключа в следующем запросе. Ввод не отображается и не сохраняется."
& ssh-add.exe $KeyPath
if ($LASTEXITCODE -ne 0) {
    throw "SSH key was not added to the ephemeral agent."
}

$state = [ordered]@{
    agent_mode = "windows-service"
    key_path = $KeyPath
    created_at_utc = [DateTime]::UtcNow.ToString("o")
}
$parent = Split-Path -Parent $StatePath
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
Write-Host "SSH key loaded into Windows OpenSSH Authentication Agent. You may close this window."
