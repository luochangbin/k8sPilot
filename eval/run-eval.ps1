<#
.SYNOPSIS
k8sPilot Phase 2 评测一键脚本：预检 -> (启动 Agent) -> 冒烟 -> 冻结基线 -> (候选对照)。

.EXAMPLE
# Agent 已手动启动，只做预检+冒烟+冻结基线
.\run-eval.ps1 -LLMApiKey "sk-xxx" -ConnectorUrl "http://10.244.0.10:8080"

.EXAMPLE
# 由脚本后台启动 Agent，并额外跑一次候选版本对照
.\run-eval.ps1 -LLMApiKey "sk-xxx" -ConnectorUrl "http://10.244.0.10:8080" -StartAgent -RunCandidate

.EXAMPLE
# 只跑冒烟，跳过基线（调试用）
.\run-eval.ps1 -SkipBaseline
#>
param(
    [string]$LLMApiKey = $env:LLM_API_KEY,
    [string]$LLMBaseUrl = "https://api.deepseek.com/v1",
    [string]$LLMModel = "deepseek-v4-flash",
    [string]$ConnectorUrl = "",
    [string]$KindName = "kind-my-cluster",
    [string]$TraceDir = "D:\AI\k8sPilot\eval-trace",
    [string]$ReportsDir = "D:\AI\k8sPilot\reports",
    [switch]$StartAgent,
    [switch]$RunCandidate,
    [switch]$SkipSmoke,
    [switch]$SkipBaseline
)

$ErrorActionPreference = "Stop"
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$AgentDir  = Join-Path $RepoRoot "agent-service"
$Python    = Join-Path $AgentDir ".venv\Scripts\python.exe"
$AgentPort = 8000
$AgentPid  = $null

function Write-Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; if ($StartAgent) { Stop-Agent } ; exit 1 }

function Invoke-Check([string]$desc, [scriptblock]$check) {
    Write-Host "  - $desc" -NoNewline
    try {
        $null = & $check
        Write-Host " OK" -ForegroundColor Green
    } catch {
        Write-Host " FAIL" -ForegroundColor Red
        throw
    }
}

function Get-LatestRun([string]$profile) {
    Get-ChildItem $ReportsDir -Directory -ErrorAction SilentlyContinue |
        Where-Object Name -like "$profile-*" |
        Sort-Object Name -Descending |
        Select-Object -First 1
}

function Start-Agent {
    New-Item -ItemType Directory -Force $TraceDir | Out-Null
    $log = Join-Path $TraceDir "agent.log"
    # Only set non-empty vars so .env (loaded by the app itself) is not
    # shadowed by empty strings.
    if ($LLMApiKey) { $env:LLM_API_KEY = $LLMApiKey }
    if ($LLMBaseUrl) { $env:LLM_BASE_URL = $LLMBaseUrl }
    if ($LLMModel) { $env:LLM_MODEL = $LLMModel }
    if ($ConnectorUrl) { $env:CONNECTOR_BASE_URL = $ConnectorUrl }
    $env:TRACE_DIR = $TraceDir
    $proc = Start-Process -FilePath $Python -ArgumentList "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "$AgentPort" `
        -WorkingDirectory $AgentDir -RedirectStandardOutput $log `
        -RedirectStandardError $log -PassThru -WindowStyle Hidden
    $script:AgentPid = $proc.Id
    Write-Host "Agent started (pid $($proc.Id)), log: $log"
}

function Stop-Agent {
    if ($script:AgentPid) {
        Stop-Process -Id $script:AgentPid -Force -ErrorAction SilentlyContinue
        Write-Host "Agent stopped (pid $($script:AgentPid))"
    }
}

function Wait-AgentHealthy {
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $r = Invoke-RestMethod -Uri "http://localhost:$AgentPort/healthz" -TimeoutSec 2
            if ($r.status -eq "ok") { return }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    Fail "Agent did not become healthy on port $AgentPort"
}

# ---------- 0. 预检 ----------
Write-Step "0. 环境预检"
Invoke-Check "python venv 存在"  { Test-Path $Python }
Invoke-Check "kubectl 可用"      { Get-Command kubectl | Out-Null; kubectl version --client | Out-Null }
Invoke-Check "kubectl 能连集群"  { kubectl get nodes }

if (-not $ConnectorUrl) {
    Write-Host "  - 自动探测 Connector Pod IP..."
    $podIp = kubectl get pod -n k8spilot -l app=ai-agent-connector -o jsonpath="{.items[0].status.podIP}" 2>$null
    if ($podIp) { $ConnectorUrl = "http://$podIp`:8080" }
}
if (-not $ConnectorUrl) { Fail "无法探测 Connector，请用 -ConnectorUrl 指定" }
Invoke-Check "Connector 健康 ($ConnectorUrl)" {
    (Invoke-WebRequest -Uri "$ConnectorUrl/healthz" -TimeoutSec 5).StatusCode -eq 200
}

if ($StartAgent) {
    Start-Agent
    Wait-AgentHealthy
} else {
    Invoke-Check "Agent 健康 (localhost:$AgentPort)" {
        (Invoke-WebRequest -Uri "http://localhost:$AgentPort/healthz" -TimeoutSec 5).StatusCode -eq 200
    }
}

New-Item -ItemType Directory -Force $ReportsDir | Out-Null

# ---------- 1. 冒烟 ----------
if (-not $SkipSmoke) {
    Write-Step "1. 冒烟（pod-oomkilled-001 x1）"
    Push-Location $RepoRoot
    try {
        & $Python -m eval run --suite phase1 --runs 1 --case pod-oomkilled-001 `
            --profile smoke --trace-dir $TraceDir --reports-dir $ReportsDir --model $LLMModel
        if ($LASTEXITCODE -ne 0) { Fail "冒烟命令退出码 $LASTEXITCODE" }
    } finally { Pop-Location }

    $smoke = Get-LatestRun "smoke"
    if (-not $smoke) { Fail "未找到 smoke 运行目录" }
    $rowsPath = Join-Path $smoke.FullName "case-results.jsonl"
    $bad = Get-Content $rowsPath | ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.verdict -in @("fixture_failed", "system_failed") }
    if ($bad) {
        Fail "冒烟失败：verdict=$($bad.verdict) error=$($bad.error)"
    }
    $traces = Get-ChildItem $TraceDir -Filter "diag_*.jsonl" -ErrorAction SilentlyContinue
    Write-Host "冒烟通过：$(Split-Path $smoke.FullName -Leaf)，Trace 文件数：$($traces.Count)"
    if (-not $traces) { Write-Host "警告：未发现 Trace 文件（检查 Agent 的 TRACE_DIR）" -ForegroundColor Yellow }
} else {
    Write-Host "跳过冒烟（-SkipSmoke）"
}

# ---------- 2. 冻结基线 ----------
if (-not $SkipBaseline) {
    Write-Step "2. 冻结基线（12 Case x5）"
    Push-Location $RepoRoot
    try {
        & $Python -m eval run --suite phase1 --runs 5 --profile baseline `
            --trace-dir $TraceDir --reports-dir $ReportsDir --model $LLMModel
        if ($LASTEXITCODE -ne 0) { Fail "基线命令退出码 $LASTEXITCODE" }
    } finally { Pop-Location }
    $baseline = Get-LatestRun "baseline"
    if (-not $baseline) { Fail "未找到 baseline 运行目录" }
    Write-Host "基线已生成：$($baseline.FullName)"
    Write-Host "查看：$(Join-Path $baseline.FullName 'report.md')"
    Write-Host "基线 run_id：$($baseline.Name)"
}

# ---------- 3. 候选对照 ----------
if ($RunCandidate) {
    Write-Step "3. 候选版本（12 Case x5）"
    Push-Location $RepoRoot
    try {
        & $Python -m eval run --suite phase1 --runs 5 --profile candidate `
            --trace-dir $TraceDir --reports-dir $ReportsDir --model $LLMModel
        if ($LASTEXITCODE -ne 0) { Fail "候选命令退出码 $LASTEXITCODE" }
    } finally { Pop-Location }
    $candidate = Get-LatestRun "candidate"
    $baselineRun = Get-LatestRun "baseline"
    if (-not $candidate -or -not $baselineRun) { Fail "缺少 baseline 或 candidate 运行目录" }

    Write-Step "4. 成对对照"
    Push-Location $RepoRoot
    try {
        & $Python -m eval compare --baseline $baselineRun.Name --candidate $candidate.Name `
            --reports-dir $ReportsDir
    } finally { Pop-Location }
}

if ($StartAgent) { Stop-Agent }
Write-Host "`n完成。" -ForegroundColor Green
