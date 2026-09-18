# Restart the Agent Service safely: stop old -> wait for port release -> start
# -> verify it is alive (process, health, listener ownership).
#
# Notes specific to this repo/machine:
# - The venv `python.exe` is a redirector; the actual listener is a *child*
#   interpreter, so listener ownership is checked against the process tree.
# - LLM_* environment variables are cleared so agent-service/.env is used.
# - Connector discovery is bounded with --request-timeout.
#
# Usage:  pwsh -File agent-service\restart-agent.ps1 [-Port 8000] [-ConnectorBaseUrl http://ip:8080]
#
# If the cluster API is unreachable, the connector Pod IP cannot be discovered;
# pass -ConnectorBaseUrl to start anyway (e.g. the last known Pod IP). Discovery
# happens *before* the old agent is stopped, so a failed lookup never leaves the
# service down.
#
# IMPORTANT: run this script directly. Do NOT pipe its output
# (e.g. `| Out-String`): the started process inherits the caller's standard
# handles, which can keep the caller's pipeline open and make the invocation
# look like it hung. If you need a log, redirect the whole invocation to a file:
#   pwsh -File restart-agent.ps1 *> restart.log

param(
    [int]$Port = 8000,
    [string]$ConnectorBaseUrl
)

$ErrorActionPreference = 'Stop'

$AgentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $AgentDir '.venv\Scripts\python.exe'
$PidFile = Join-Path $AgentDir '.agent.pid'
$OutLog = 'D:\AI\k8sPilot\eval-trace\agent.out.log'
$ErrLog = 'D:\AI\k8sPilot\eval-trace\agent.err.log'

if (-not (Test-Path $Python)) { throw "venv python not found: $Python" }

function Get-ProcessTree([int]$RootPid) {
    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
    $seen = @($RootPid)
    $queue = @($RootPid)
    while ($queue.Count -gt 0) {
        $next = @()
        foreach ($parent in $queue) {
            foreach ($child in ($all | Where-Object { $_.ParentProcessId -eq $parent })) {
                if ($seen -notcontains $child.ProcessId) {
                    $seen += $child.ProcessId
                    $next += $child.ProcessId
                }
            }
        }
        $queue = $next
    }
    return $seen
}

# 1. Resolve the connector address (bounded) BEFORE stopping anything: a failed
#    lookup must not take the running service down.
if (-not $ConnectorBaseUrl) {
    $podIp = kubectl --request-timeout=10s get pod -n k8spilot -l app=ai-agent-connector `
        -o jsonpath="{.items[0].status.podIP}" 2>$null
    if (-not $podIp) {
        throw 'Cannot resolve ai-agent-connector Pod IP (kubectl failed or no pod); ' +
              'pass -ConnectorBaseUrl to start with a known address'
    }
    $ConnectorBaseUrl = "http://$podIp`:8080"
}
$env:CONNECTOR_BASE_URL = $ConnectorBaseUrl
Remove-Item Env:LLM_API_KEY, Env:LLM_BASE_URL, Env:LLM_MODEL -ErrorAction SilentlyContinue

# 2. Stop old agents (all uvicorn app.main:app processes).
$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'uvicorn app\.main:app' }
foreach ($p in $old) {
    Write-Output "Stopping old Agent pid=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

# 3. Wait (bounded) until the port is actually released.
for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        break
    }
    Start-Sleep -Milliseconds 250
}
$stale = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($stale) {
    throw "Port $Port is still occupied by pid=$($stale.OwningProcess); refusing to start"
}

# 4. Start the new Agent. Stdin is redirected from an empty file so the child
#    never inherits (and holds open) the caller's standard handles.
$emptyStdin = Join-Path ([System.IO.Path]::GetTempPath()) 'agent-empty-stdin.txt'
if (-not (Test-Path $emptyStdin)) { New-Item -ItemType File -Path $emptyStdin -Force | Out-Null }
$a = Start-Process -FilePath $Python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$Port" `
    -WorkingDirectory $AgentDir `
    -RedirectStandardInput $emptyStdin `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru -WindowStyle Hidden
Set-Content -Path $PidFile -Value $a.Id

# 5. Health check, failing fast if the process died.
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    if ($a.HasExited) {
        Write-Output '=== agent.err.log (tail) ==='
        Get-Content $ErrLog -Tail 50 -ErrorAction SilentlyContinue
        throw "Agent exited during startup (ExitCode=$($a.ExitCode)); see $ErrLog"
    }
    try {
        if ((Invoke-RestMethod "http://localhost:$Port/healthz" -TimeoutSec 1).status -eq 'ok') {
            $healthy = $true
            break
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $healthy) { throw "Agent health check timed out on port $Port" }

# 6. The listener must belong to the new process tree (venv spawns a child).
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
$tree = Get-ProcessTree $a.Id
if ($tree -notcontains [int]$listener.OwningProcess) {
    throw "Wrong process is listening on $Port : expected tree of pid=$($a.Id) $(($tree -join ',')), actual=$($listener.OwningProcess)"
}

Write-Output "Agent restarted: pid=$($a.Id) listener=$($listener.OwningProcess) connector=$ConnectorBaseUrl port=$Port"
