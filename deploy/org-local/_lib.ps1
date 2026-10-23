# Shared by up.ps1, down.ps1 and smoke.ps1. Windows PowerShell 5.1+ and PowerShell 7
# (Windows, macOS, Linux). Keep this file ASCII: 5.1 reads scripts without a BOM as ANSI.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:OrgDir = $PSScriptRoot
$script:OrgEnvFile = Join-Path $OrgDir '.env.org'
$script:OrgStateDir = Join-Path $OrgDir '.state'
$script:OrgComposeFile = Join-Path $OrgDir 'docker-compose.yml'
# Every compose call names this project explicitly (-p), so nothing else can redirect it.
$script:OrgProject = 'agentic-org'
# docker (default) or podman, as in scripts/check.py. Both are driven as '<engine> compose'.
$script:OrgEngine = if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { 'docker' }
# The gateway's LITE dashboard set (spec 25), copied from its checkout into .state.
$script:OrgLiteDashboards = @('live', 'requests', 'billing', 'latency-breakup', 'sla', 'trust-safety', 'trends', 'quarterly')

# Compose settings exported for another stack must never steer these scripts.
foreach ($name in @('COMPOSE_PROJECT_NAME', 'COMPOSE_FILE', 'COMPOSE_PROFILES')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}

function Stop-Org([string]$Message) {
    Write-Host "org: $Message" -ForegroundColor Red
    exit 1
}

function Assert-OrgEngine {
    if ($OrgEngine -notin @('docker', 'podman')) {
        Stop-Org "CONTAINER_ENGINE=$OrgEngine`: use docker or podman"
    }
    if (-not (Get-Command $OrgEngine -ErrorAction SilentlyContinue)) {
        Stop-Org "'$OrgEngine' not found (set CONTAINER_ENGINE=docker or podman)"
    }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $OrgEngine compose version 2>$null | Out-Null } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) {
        Stop-Org "'$OrgEngine compose' is not available (Docker Compose v2, or podman with a compose provider)"
    }
}

# The gateway's checkout (read only): TOKENLEAN_DIR, holding deploy/lite/local.
function Get-OrgTlDir {
    $TlDir = $env:TOKENLEAN_DIR
    if (-not $TlDir) { Stop-Org 'set TOKENLEAN_DIR to a TokenLean checkout that has deploy/lite/local (see README)' }
    if (-not (Test-Path (Join-Path $TlDir 'deploy/lite/local/up.ps1'))) {
        Stop-Org "TOKENLEAN_DIR=$TlDir has no deploy/lite/local/up.ps1"
    }
    return $TlDir
}

# KEY=value pairs of an env file, read literally (CR dropped).
function Read-OrgEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path $Path)) { return $values }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $l = $line.TrimEnd("`r")
        if ($l -eq '' -or $l.StartsWith('#')) { continue }
        $i = $l.IndexOf('=')
        if ($i -lt 1) { continue }
        $values[$l.Substring(0, $i)] = $l.Substring($i + 1)
    }
    return $values
}

# A value from the LITE stack's .env.lite.
function Get-OrgLiteValue([string]$TlDir, [string]$Key) {
    $path = Join-Path $TlDir 'deploy/lite/local/.env.lite'
    if (-not (Test-Path $path)) { Stop-Org 'the LITE stack has no .env.lite yet - its up did not run' }
    return (Read-OrgEnv $path)[$Key]
}

function Get-OrgValue([string]$Key) {
    return (Read-OrgEnv $OrgEnvFile)[$Key]
}

# Write text with LF line ends and no BOM (compose and bash read the file too).
function Write-OrgFile([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}

# Set KEY in .env.org: only where it is empty or missing, unless -Overwrite.
function Set-OrgValue([string]$Key, [string]$Value, [switch]$Overwrite) {
    $lines = [Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in [IO.File]::ReadAllLines($OrgEnvFile)) {
        $l = $line.TrimEnd("`r")
        if ($l.StartsWith("$Key=")) {
            $found = $true
            if ($Overwrite -or $l -eq "$Key=") { $l = "$Key=$Value" }
        }
        $lines.Add($l)
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    Write-OrgFile $OrgEnvFile (($lines -join "`n") + "`n")
}

# Run '<engine> compose -p agentic-org -f docker-compose.yml --env-file .env.org <args>';
# returns the exit code. Native stderr (compose's progress) is not an error here.
function Invoke-OrgCompose([string[]]$ComposeArgs) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $OrgEngine compose -p $script:OrgProject -f $OrgComposeFile --env-file $OrgEnvFile @ComposeArgs | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return $code
}

function Get-OrgUrl([string]$Key, [string]$Default) {
    $port = Get-OrgValue $Key
    if (-not $port) { $port = $Default }
    return "http://127.0.0.1:$port"
}

# A random hex string of $Bytes bytes from the OS's cryptographic generator.
function New-OrgSecret([int]$Bytes = 24) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

# GET (or POST a JSON body) and return the answer's text, '' when there is none.
function Invoke-OrgHttp([string]$Url, [hashtable]$Headers = @{}, [string]$Body = $null) {
    try {
        if ($Body) {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -Method Post `
                -ContentType 'application/json' -Body $Body -TimeoutSec 15
        } else {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -TimeoutSec 15
        }
        return [string]$r.Content
    } catch {
        return ''
    }
}
