<#
.SYNOPSIS
  Install the umbrella chart on a throw-away kind cluster and run its smoke (M3-02).
.DESCRIPTION
  The PowerShell twin of smoke.sh (Windows PowerShell 5.1 and PowerShell 7). kind, kubectl
  and helm run in a pinned tool container (tools.Dockerfile) that drives this machine's
  Docker through its socket: nothing native is installed. Docker only.
    $env:SKIP_BUILD = '1'; $env:CP_IMAGE = '...'   use an image already built here
    $env:KEEP = '1'                                 keep the cluster afterwards
    $env:ENVIRONMENT = 'staging'                    install with environments/staging.yaml
                                                    and run the promotion dry run (M3-04)
#>
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here '..\..\..')).Path
$Engine = if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { 'docker' }
if (-not $env:KIND_CLUSTER) { $env:KIND_CLUSTER = 'agentic-kind-smoke' }
if (-not $env:CP_IMAGE) { $env:CP_IMAGE = 'agentic-control-plane:kind-smoke' }
if (-not $env:KEEP) { $env:KEEP = '0' }

# Native tools write progress and warnings to stderr; only the exit code means failure
# (Windows PowerShell 5.1 would otherwise turn redirected stderr into a stopping error).
function Invoke-Engine {
    $ErrorActionPreference = 'Continue'
    & $Engine @args
    if ($LASTEXITCODE -ne 0) { throw "$Engine $($args -join ' ') failed ($LASTEXITCODE)" }
}

if ($env:SKIP_BUILD -ne '1') {
    Invoke-Engine build -f (Join-Path $Root 'control-plane\Dockerfile') -t $env:CP_IMAGE $Root
}
$hash = (Get-FileHash -Algorithm SHA256 (Join-Path $Here 'tools.Dockerfile')).Hash.ToLower()
$tools = "agentic-kind-tools:$($hash.Substring(0, 12))"
Invoke-Engine build -q -t $tools -f (Join-Path $Here 'tools.Dockerfile') $Here | Out-Null

$name = "$($env:KIND_CLUSTER)-tools-$PID"
Invoke-Engine create --name $name -v /var/run/docker.sock:/var/run/docker.sock `
    -e KIND_CLUSTER -e CP_IMAGE -e KEEP -e KIND_NODE_IMAGE -e ENVIRONMENT $tools sh /w/kind/inside.sh | Out-Null
try {
    Invoke-Engine cp (Join-Path $Root 'deploy\helm\.') "${name}:/w/"
    Invoke-Engine cp (Join-Path $Root 'deploy\org-local\cp-postgres\init.sh') "${name}:/w/kind/pg-init.sh"
    Invoke-Engine start -a $name
}
finally {
    $ErrorActionPreference = 'Continue'
    & $Engine rm -f $name 2>$null | Out-Null
    # an interrupted run leaves the cluster's node behind
    if ($env:KEEP -ne '1') { & $Engine rm -f "$($env:KIND_CLUSTER)-control-plane" 2>$null | Out-Null }
}
