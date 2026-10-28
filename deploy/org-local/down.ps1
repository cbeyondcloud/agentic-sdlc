<#
.SYNOPSIS
  Stop the local org stack, then the gateway's LITE stack. Data volumes are kept.

.DESCRIPTION
  $env:TOKENLEAN_DIR = 'D:\token-optimisation'; .\down.ps1 [-Purge] [-KeepLite]

  -Purge     also delete the volumes (control-plane database, Grafana; and the LITE
             stack's). .env.org is kept, so the next up.ps1 reuses its passwords.
  -KeepLite  leave the gateway's LITE stack running

  Engine: $env:CONTAINER_ENGINE = 'docker' (default) or 'podman'.
#>
param([switch]$Purge, [switch]$KeepLite)
. (Join-Path $PSScriptRoot '_lib.ps1')

Assert-OrgEngine
if (-not (Test-Path $OrgEnvFile)) { Stop-Org '.env.org not found - nothing was started from here' }

$downArgs = @('down', '--remove-orphans')
if ($Purge) { $downArgs += '--volumes' }
if ((Invoke-OrgCompose $downArgs) -ne 0) { Stop-Org 'compose down failed' }
Write-Host ('org: stopped' + $(if ($Purge) { ' and purged' } else { '' }))

if (-not $KeepLite) {
    $TlDir = Get-OrgTlDir
    $env:CONTAINER_ENGINE = $OrgEngine
    $liteDown = Join-Path $TlDir 'deploy/lite/local/down.ps1'
    if ($Purge) { & $liteDown -Purge } else { & $liteDown }
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Stop-Org "the LITE stack did not stop (exit $LASTEXITCODE)" }
}
