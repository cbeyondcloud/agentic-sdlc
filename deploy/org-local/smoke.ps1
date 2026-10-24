<#
.SYNOPSIS
  Smoke-test a running local org stack. Exit 0 when every check passes.

.DESCRIPTION
  $env:TOKENLEAN_DIR = 'D:\token-optimisation'; .\smoke.ps1

  Checks: the control plane is live and ready (database migrated, isolation enforced);
  Grafana is healthy, both datasources answer, the seven framework dashboards and the
  gateway's LITE set are provisioned; the gateway answers; and one tagged call through the
  gateway (mock mode: no provider, no spend) shows up in the tenant's dash_calls view, read
  through Grafana's datasource as the tenant's reader. HTTP only, engine-independent.
#>
. (Join-Path $PSScriptRoot '_lib.ps1')

if (-not (Test-Path $OrgEnvFile)) { Stop-Org '.env.org not found - run up.ps1 first' }
$TlDir = Get-OrgTlDir

$cp = Get-OrgUrl 'ORG_CP_PORT' '8280'
$grafana = Get-OrgUrl 'ORG_GRAFANA_PORT' '3300'
$litePort = Get-OrgLiteValue $TlDir 'LITE_PROXY_PORT'
if (-not $litePort) { $litePort = '4100' }
$gateway = "http://127.0.0.1:$litePort"
$mode = (Read-OrgEnv (Join-Path $TlDir 'deploy/lite/local/.state/stack.env'))['LITE_MODE']
if ($mode -ne 'mock') { Stop-Org 'the LITE stack is not in mock mode - start it with up.ps1' }

# Credentials stay in this process (request headers), never on a command line.
$pair = 'admin:' + (Get-OrgValue 'GRAFANA_ADMIN_PASSWORD')
$grafanaAuth = @{ Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair)) }
$run = "smoke-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())-$PID"
$script:fails = 0

function Pass([string]$What) { Write-Host "PASS  $What" }
function Fail([string]$What, [string]$Why = '') {
    if ($Why) { Write-Host "FAIL  $What ($Why)" } else { Write-Host "FAIL  $What" }
    $script:fails++
}
function Count([string]$Text, [string]$Pattern) { return ([regex]::Matches($Text, [regex]::Escape($Pattern))).Count }

Write-Host "org smoke: control plane $cp  grafana $grafana  gateway $gateway  run $run"

# 1. control plane
if ((Invoke-OrgHttp "$cp/healthz").Contains('"status":"ok"')) { Pass 'control plane live' } else { Fail 'control plane live' }
$body = Invoke-OrgHttp "$cp/readyz"
if ($body.Contains('"db_enforced":true')) { Pass 'control plane ready (migrated, isolation enforced)' } else { Fail 'control plane ready' $body }

# 2. Grafana, its datasources and dashboards
$body = Invoke-OrgHttp "$grafana/api/health"
if ($body -match '"database"\s*:\s*"ok"') { Pass 'grafana healthy' } else { Fail 'grafana healthy' }
foreach ($uid in @('agentic-postgres', 'app-postgres')) {
    $body = Invoke-OrgHttp "$grafana/api/datasources/uid/$uid/health" $grafanaAuth
    if ($body.Contains('"status":"OK"')) { Pass "datasource $uid answers" } else { Fail "datasource $uid answers" $body }
}
$n = Count (Invoke-OrgHttp "$grafana/api/search?tag=agentic&type=dash-db" $grafanaAuth) '"uid":"agentic-'
if ($n -eq 7) { Pass '7 framework dashboards provisioned' } else { Fail '7 framework dashboards provisioned' "found $n" }
$n = Count (Invoke-OrgHttp "$grafana/api/search?type=dash-db&query=" $grafanaAuth) '"folderTitle":"Gateway"'
$expected = $OrgLiteDashboards.Count
if ($n -eq $expected) { Pass "$expected gateway LITE dashboards provisioned" } else { Fail 'gateway LITE dashboards provisioned' "found $n of $expected" }

# 3. the gateway, and one tagged call through it
if ((Invoke-OrgHttp "$gateway/health").Contains('"status":"ok"')) { Pass 'gateway healthy' } else { Fail 'gateway healthy' }
$callHeaders = @{
    Authorization       = 'Bearer ' + (Get-OrgLiteValue $TlDir 'TOKENLEAN_API_KEY')
    'X-Agentic-Run-Id'  = $run
    'X-Agentic-Step'    = 'smoke'
    'X-Agentic-Agent'   = 'smoke@1.0.0'
}
$call = '{"model":"lite-mock-1","max_tokens":8,"messages":[{"role":"user","content":"ping ' + $run + '"}]}'
if (Invoke-OrgHttp "$gateway/v1/chat/completions" $callHeaders $call) { Pass 'tagged call through the gateway (mock)' } else { Fail 'tagged call through the gateway' }

# 4. the call in the tenant's dashboard view, through Grafana's datasource (the reader)
$sql = "SELECT count(*) AS n FROM dash_calls WHERE run_id = '$run'"
$query = '{"from":"now-1h","to":"now","queries":[{"refId":"A","format":"table","rawQuery":true,"datasource":{"uid":"agentic-postgres"},"rawSql":"' + $sql + '"}]}'
$seen = $false
for ($i = 0; $i -lt 10 -and -not $seen; $i++) {
    $body = Invoke-OrgHttp "$grafana/api/ds/query" $grafanaAuth $query
    if ($body -match '"values"\s*:\s*\[\s*\[\s*1\s*\]\s*\]') { $seen = $true } else { Start-Sleep -Seconds 3 }
}
if ($seen) { Pass 'the call is in dash_calls for the tenant' } else { Fail 'the call is in dash_calls' $body }

if ($script:fails -gt 0) {
    Write-Host "org smoke: $($script:fails) check(s) failed"
    exit 1
}
Write-Host 'org smoke: all checks passed'
