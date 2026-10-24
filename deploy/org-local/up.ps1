<#
.SYNOPSIS
  Start the local org stack: the gateway's LITE stack (mock mode: no provider key, no
  spend), then the control plane, its Postgres and Grafana.

.DESCRIPTION
  $env:TOKENLEAN_DIR = 'D:\token-optimisation'; .\up.ps1 [-NoBuild]

  -NoBuild  do not rebuild the control-plane and gateway proxy images

  TOKENLEAN_DIR is a TokenLean checkout with deploy/lite/local; it is only read (its own up
  writes its git-ignored .env.lite and .state). First run: creates .env.org from
  .env.org.template and generates its passwords. Engine: $env:CONTAINER_ENGINE = 'docker'
  (default) or 'podman'. If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\up.ps1
#>
param([switch]$NoBuild)
. (Join-Path $PSScriptRoot '_lib.ps1')

Assert-OrgEngine
$TlDir = Get-OrgTlDir

# 1. The gateway's LITE stack, started the way it documents (ruling S1).
Write-Host "org: starting the gateway's LITE stack (mock mode) from $TlDir"
$env:CONTAINER_ENGINE = $OrgEngine
$liteUp = Join-Path $TlDir 'deploy/lite/local/up.ps1'
if ($NoBuild) { & $liteUp -Mock -NoBuild } else { & $liteUp -Mock }
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Stop-Org "the LITE stack did not start (exit $LASTEXITCODE)" }

# 2. Settings and secrets.
if (-not (Test-Path $OrgEnvFile)) {
    Write-OrgFile $OrgEnvFile ([IO.File]::ReadAllText((Join-Path $OrgDir '.env.org.template')))
    Write-Host 'org: created .env.org'
}
foreach ($key in @('CP_SUPERUSER_PASSWORD', 'CP_OWNER_PASSWORD', 'CP_APP_PASSWORD',
        'AGENTIC_DASH_READER_PASSWORD', 'GRAFANA_ADMIN_PASSWORD')) {
    Set-OrgValue $key (New-OrgSecret 24)
}
$gatewayTenant = Get-OrgLiteValue $TlDir 'LITE_TENANT_ID'
if (-not $gatewayTenant) { $gatewayTenant = 'LITE-LOC-01' }
Set-OrgValue 'GATEWAY_TENANT' $gatewayTenant -Overwrite
Set-OrgValue 'GATEWAY_DB_PASSWORD' (Get-OrgLiteValue $TlDir 'DB_PASSWORD') -Overwrite

# 3. The gateway's LITE dashboards, copied (never mounted from its checkout).
$copies = Join-Path $OrgStateDir 'gateway-dashboards'
New-Item -ItemType Directory -Force $copies | Out-Null
Get-ChildItem $copies -Filter '*.json' | Remove-Item -Force
foreach ($name in $OrgLiteDashboards) {
    Copy-Item (Join-Path $TlDir "dashboard/dashboards/$name.json") (Join-Path $copies "$name.json")
}

# 4. Our services.
$upArgs = @('up', '-d', '--remove-orphans')
if (-not $NoBuild) { $upArgs += '--build' }
Write-Host "org: starting the control plane, its Postgres and Grafana with $OrgEngine ..."
if ((Invoke-OrgCompose $upArgs) -ne 0) {
    Invoke-OrgCompose @('logs', '--tail', '60', 'cp-init') | Out-Null
    Stop-Org "compose up failed (cp-init's log is above)"
}

$cp = Get-OrgUrl 'ORG_CP_PORT' '8280'
$grafana = Get-OrgUrl 'ORG_GRAFANA_PORT' '3300'
$timeout = if ($env:ORG_HEALTH_TIMEOUT) { [int]$env:ORG_HEALTH_TIMEOUT } else { 240 }
function Wait-OrgUrl([string]$Url, [string]$Expect) {
    $deadline = (Get-Date).AddSeconds($timeout)
    Write-Host -NoNewline "org: waiting for $Url "
    while ($true) {
        $body = Invoke-OrgHttp $Url
        if ($body.Contains($Expect)) { break }
        if ((Get-Date) -gt $deadline) {
            Write-Host ''
            Invoke-OrgCompose @('ps') | Out-Null
            Stop-Org "$Url did not answer within ${timeout}s"
        }
        Write-Host -NoNewline '.'
        Start-Sleep -Seconds 3
    }
    Write-Host ' ok'
}
Wait-OrgUrl "$cp/readyz" '"db_enforced":true'
Wait-OrgUrl "$grafana/api/health" '"database"'

Write-Host "org: control plane  $cp  (tenant $(Get-OrgValue 'ORG_TENANT'))"
Write-Host "org: grafana        $grafana  (admin; password GRAFANA_ADMIN_PASSWORD in $OrgEnvFile)"
Write-Host "org: admin token    see README (admin issue-token --tenant $(Get-OrgValue 'ORG_TENANT'))"
Write-Host 'org: next           .\smoke.ps1'
