<#
.SYNOPSIS
启动 k8sPilot Agent Service（本地/开发/评测用）。
配置从 agent-service 目录下的 .env 读取；Connector 地址留空时自动探测 Pod IP。
优先级：-xxx 参数 > .env > 系统环境变量 > 代码默认值。

.EXAMPLE
# 前台启动（看日志，Ctrl+C 停止）
.\start-agent.ps1

.EXAMPLE
# 后台启动（日志落盘，停止：Stop-Process -Id (Get-Content .agent.pid)）
.\start-agent.ps1 -Background
#>
param(
    [string]$LLMApiKey = "",
    [string]$LLMBaseUrl = "",
    [string]$LLMModel = "",
    [string]$ConnectorUrl = "",
    [string]$TraceDir = "",
    [int]$Port = 8001,
    [switch]$NoTrace,
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$AgentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $AgentDir
$Python   = Join-Path $AgentDir ".venv\Scripts\python.exe"
$EnvFile  = Join-Path $AgentDir ".env"

if (-not (Test-Path $Python)) {
    Write-Host "未找到 venv：$Python，请先执行  python -m venv .venv && .\.venv\Scripts\python -m pip install -e '.[dev]'"
    exit 1
}

# ---- 读取 .env（简单解析：key=value，忽略注释/空行/引号）----
$dotenv = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $eq = $line.IndexOf("=")
            if ($eq -gt 0) {
                $k = $line.Substring(0, $eq).Trim()
                $v = $line.Substring($eq + 1).Trim().Trim('"', "'")
                $dotenv[$k] = $v
            }
        }
    }
}

# ---- 解析最终配置：参数 > .env > 系统环境变量 > 默认 ----
if (-not $LLMApiKey)   { $LLMApiKey   = if ($dotenv["LLM_API_KEY"])     { $dotenv["LLM_API_KEY"] }     else { $env:LLM_API_KEY } }
if (-not $LLMBaseUrl)  { $LLMBaseUrl  = if ($dotenv["LLM_BASE_URL"])    { $dotenv["LLM_BASE_URL"] }    else { "https://api.deepseek.com/v1" } }
if (-not $LLMModel)    { $LLMModel    = if ($dotenv["LLM_MODEL"])       { $dotenv["LLM_MODEL"] }       else { "deepseek-v4-flash" } }
if (-not $ConnectorUrl) { $ConnectorUrl = if ($dotenv["CONNECTOR_BASE_URL"]) { $dotenv["CONNECTOR_BASE_URL"] } else { "" } }
if (-not $TraceDir)    { $TraceDir    = if ($dotenv["TRACE_DIR"])       { $dotenv["TRACE_DIR"] }       else { "D:\AI\k8sPilot\eval-trace" } }
$DbPath = if ($dotenv["DIAGNOSIS_DB"]) { $dotenv["DIAGNOSIS_DB"] } else { "" }

# ---- Connector 地址：参数/.env > kubectl 自动探测 > localhost:8080 ----
if (-not $ConnectorUrl) {
    try {
        $podIp = kubectl get pod -n k8spilot -l app=ai-agent-connector -o jsonpath="{.items[0].status.podIP}" 2>$null
        if ($podIp) { $ConnectorUrl = "http://$podIp`:8080" }
    } catch { }
}
if (-not $ConnectorUrl) { $ConnectorUrl = "http://localhost:8080" }

if (-not $LLMApiKey) {
    Write-Host "警告：未设置 LLM_API_KEY（本地无需鉴权的 LLM 端点可忽略，否则诊断会 401）" -ForegroundColor Yellow
}

# ---- 写环境变量（仅非空才设置，避免空值覆盖 .env）----
if ($LLMApiKey)  { $env:LLM_API_KEY = $LLMApiKey }
if ($LLMBaseUrl) { $env:LLM_BASE_URL = $LLMBaseUrl }
if ($LLMModel)   { $env:LLM_MODEL = $LLMModel }
$env:CONNECTOR_BASE_URL = $ConnectorUrl
if ($DbPath) { $env:DIAGNOSIS_DB = $DbPath }
if ($NoTrace) { Remove-Item Env:TRACE_DIR -ErrorAction SilentlyContinue }
else {
    New-Item -ItemType Directory -Force $TraceDir | Out-Null
    $env:TRACE_DIR = $TraceDir
}

Write-Host "Agent Service 配置（来源：$EnvFile + 参数/探测）："
Write-Host "  LLM_BASE_URL       = $LLMBaseUrl"
Write-Host "  LLM_MODEL          = $LLMModel"
Write-Host "  CONNECTOR_BASE_URL = $ConnectorUrl"
Write-Host "  TRACE_DIR          = $(if ($NoTrace) { '(disabled)' } else { $TraceDir })"
Write-Host "  端口               = $Port"

if ($Background) {
    $logBase = if ($NoTrace) { $AgentDir } else { $TraceDir }
    $outLog = Join-Path $logBase "agent.out.log"
    $errLog = Join-Path $logBase "agent.err.log"
    $proc = Start-Process -FilePath $Python -ArgumentList "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "$Port" `
        -WorkingDirectory $AgentDir -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog -PassThru -WindowStyle Hidden
    Set-Content -Path (Join-Path $AgentDir ".agent.pid") -Value $proc.Id
    Write-Host "后台已启动 pid=$($proc.Id)，日志：$outLog（stderr: $errLog）"
    Write-Host "停止：Stop-Process -Id (Get-Content .agent.pid)"
} else {
    Write-Host "启动（Ctrl+C 停止）..."
    & $Python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
}
