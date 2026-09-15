# k8sPilot

> 在 Headlamp 资源详情页内，用可追溯的实时证据完成 Kubernetes 故障诊断与根因定位。

![Kubernetes](https://img.shields.io/badge/Kubernetes-AIOps-326CE5?logo=kubernetes&logoColor=white)
![Headlamp](https://img.shields.io/badge/Headlamp-Plugin-4F46E5)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Go](https://img.shields.io/badge/Go-1.26-00ADD8?logo=go&logoColor=white)
![Status](https://img.shields.io/badge/status-active_development-F59E0B)

[诊断效果](#诊断效果) · [核心能力](#核心能力) · [工作方式](#工作方式) · [快速开始](#快速开始) · [Agent 评测](#可重复的-agent-评测) · [模型对比](#模型对比2026-09-15) · [路线图](#路线图) · [项目文档](#项目文档)

k8sPilot 是一个面向单集群 Kubernetes 的 **Headlamp 智能诊断插件**。运维人员可以直接在 Pod、Deployment、Node 或 PVC 详情页点击「智能诊断」，由 LLM Agent 按需查询资源状态、关联关系、Events、日志与指标，最终返回结构化的症状、调查过程、Root Cause、Evidence、置信度和修复建议。

它不是一个只会解释报错的聊天机器人：**Connector 负责获取事实，Agent 负责形成并验证假设，Eval Harness 负责证明诊断能力是否真的提升。**

## 诊断效果

在 Headlamp 资源详情页中直接查看调查过程、Root Cause、实时证据、知识参考和修复建议。点击图片可查看完整诊断结果。

| ConfigMap 缺失 | CPU 请求过高 | CrashLoopBackOff |
|:---:|:---:|:---:|
| [![ConfigMap 缺失诊断结果](docs/images/pod-missing-configmap-diagnosis.png)](docs/images/pod-missing-configmap-diagnosis.png) | [![CPU 请求过高诊断结果](docs/images/pod-insufficient-cpu-diagnosis.png)](docs/images/pod-insufficient-cpu-diagnosis.png) | [![CrashLoopBackOff 诊断结果](docs/images/pod-crashloop-diagnosis.png)](docs/images/pod-crashloop-diagnosis.png) |

## 核心能力

- **Headlamp 原生入口**：无需切换到独立控制台，在资源详情页发起诊断并查看结果。
- **Agentic 故障调查**：Agent 根据已获得的证据动态选择 `inspect`、`relations`、`events`、`logs`、`query_metrics` 和 `query_logs`，而不是一次性抓取全部集群数据。
- **多源实时证据**：融合 Kubernetes API、Pod Logs、Prometheus 指标和 Loki 日志；外部数据源不可用时自动降级到 Kubernetes-only 路径。
- **知识与经验增强**：检索 Runbook、已知问题和已验证的历史 Incident，辅助形成假设和选择调查路径。
- **可解释诊断结果**：Root Cause 必须由实时 Evidence 支撑；证据不足时明确返回缺失证据，不编造唯一结论。
- **可重复能力评测**：通过故障注入、Trace、自动评分、冻结基线和候选版本对照，量化准确率、证据质量、成本与延迟。
- **最小权限边界**：Agent 不持有 kubeconfig；集群访问集中在使用只读 ServiceAccount 的 Connector 中。

## 工作方式

```mermaid
flowchart LR
    User["SRE / Developer"] --> Headlamp["Headlamp<br/>AI Diagnosis Plugin"]
    Headlamp --> Agent["Agent Service<br/>Session + Tool Calling"]
    Agent <--> LLM["OpenAI-compatible LLM"]
    Agent --> Knowledge["Knowledge & Incident Store"]
    Agent --> Connector["Read-only Connector"]
    Connector --> K8s["Kubernetes API"]
    Connector --> Prometheus["Prometheus"]
    Connector --> Loki["Loki"]
    Eval["Eval Harness"] --> Agent
```

一次诊断遵循以下链路：

```text
Headlamp 中点击「智能诊断」
  → 创建 Diagnosis Session
  → Agent 形成初始假设
  → 按需调用 Connector 获取实时事实
  → 用新证据验证、修正或放弃假设
  → 必要时检索历史 Incident / Runbook
  → 输出 Root Cause、Evidence、Confidence 与 Recommendations
```

系统使用固定的信息优先级：

```text
实时 Tool Evidence > 环境配置事实 > 已验证历史 Incident > 文档知识 > 模型自身知识
```

RAG 和历史案例只能辅助调查，不能替代当前集群中的实时证据。

## 组件

| 目录 | 组件 | 技术栈 | 职责 |
|---|---|---|---|
| `headlamp-plugin/ai-diagnosis-plugin/` | Headlamp 插件 | TypeScript / React | 诊断入口、进度轮询与结构化结果展示 |
| `agent-service/` | Agent Service | Python / FastAPI | Diagnosis API、Agent Loop、Session、Trace、历史与知识检索 |
| `connector/` | ai-agent-connector | Go | 使用只读 RBAC 查询 Kubernetes、Prometheus 与 Loki |
| `eval/` | Eval Harness | Python | Case、故障注入、Runner、Scorer、Reporter 与版本对照 |
| `observability/` | 可选数据源 | Prometheus / Loki / Fluent Bit | 提供指标和历史日志证据 |

## 快速开始

下面以 **kind + Windows PowerShell 7** 为例，先跑通 Kubernetes-only 诊断闭环。Prometheus、Loki 和知识库均为可选增强能力。

### 前置条件

- 一个可访问的 Kubernetes 集群，以及可用的 `kubectl`
- Docker；使用 kind 时还需要 `kind`
- Python 3.11+
- Node.js 与 npm
- 一个支持 Function Calling 的 OpenAI-compatible LLM 接口

### 1. 构建并部署只读 Connector

```powershell
docker build -t ai-agent-connector:phase3 ./connector
kind load docker-image ai-agent-connector:phase3 --name <cluster-name>
kubectl apply -f connector/deploy/connector-all.yaml
kubectl -n k8spilot rollout status deployment/ai-agent-connector
```

将 Connector 转发到本机：

```powershell
kubectl -n k8spilot port-forward service/ai-agent-connector 8080:8080
```

### 2. 配置并启动 Agent Service

Agent Service 只读取自身目录下的 `.env`。复制模板后填写配置；`.env` 已被 `.gitignore` 排除，不要提交 API Key。

```powershell
cd agent-service
Copy-Item .env.template .env
```

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<your-api-key>
LLM_MODEL=<model-with-function-calling>
CONNECTOR_BASE_URL=http://localhost:8080
```

在该目录安装依赖并启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\start-agent.ps1
```

确认服务可用：

```powershell
Invoke-RestMethod http://localhost:8000/healthz
```

### 3. 构建并安装 Headlamp 插件

```powershell
cd headlamp-plugin/ai-diagnosis-plugin
npm install
npm run build

$pluginDir = Join-Path $env:APPDATA 'Headlamp\Config\plugins\ai-diagnosis-plugin'
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
Copy-Item -Path '.\dist\*' -Destination $pluginDir -Recurse -Force
```

确认 `%APPDATA%\Headlamp\Config\plugins\ai-diagnosis-plugin\main.js` 已生成，然后重启 Headlamp。进入 Pod、Deployment、Node 或 PVC 详情页，点击「智能诊断」。插件是由 Headlamp 直接加载的前端构建产物，不需要单独运行插件服务；Agent Service 仍需保持运行。

只有开发插件、需要监听源码变化和热加载时才执行：

```powershell
npm start
```

完整的故障注入与端到端验收步骤见 [Phase 1 E2E 文档](docs/phase1-e2e.md)。

### 4. 启用指标与日志增强（可选）

```powershell
kubectl apply -f observability/observability-all.yaml
kubectl -n observability get pods
```

Connector 会通过 `/capabilities` 暴露可用数据源。Prometheus 或 Loki 不可用时，诊断仍会继续，并在结果中记录降级原因。详细部署与验收方式见 [Phase 3 部署文档](docs/phase3-deploy.md)。

### 5. 导入并启用知识库（可选）

Phase 4 使用本地 SQLite FTS5 保存 Runbook、Known Issue 和人工验证过的 Incident。当前支持复用由 k8sPilot 生成的兼容 `knowledge.db`，或先把已有资料映射为 `KnowledgeDocument` / `IncidentCase` 种子数据，再执行内置摄取命令：

```powershell
cd agent-service
$knowledgeDb = Join-Path (Resolve-Path ..) 'data\knowledge.db'
.\.venv\Scripts\python -m app.knowledge.ingest --db $knowledgeDb --show
```

随后在 `agent-service/.env` 中设置知识库的绝对路径并重启 Agent Service：

```dotenv
KNOWLEDGE_DB=D:\AI\k8sPilot\data\knowledge.db
```

当前 CLI 不会直接扫描任意 Markdown 目录，也不接受 JSON、YAML、网页或第三方数据库。已有资料的字段映射、种子示例、重复导入规则和验收步骤见 [知识增强文档](docs/phase4-knowledge.md#导入已有知识库)。

## 可重复的 Agent 评测

k8sPilot 把评测作为产品能力的一部分，而不是只依赖人工观察几次输出。

```text
Versioned Cases
  → Fault Injector
  → Diagnosis Runner
  → Trace Collector
  → Scorer
  → Report / Baseline Comparison
```

当前 Phase 2 冻结基线包含 12 个故障 Case、每个 Case 运行 5 次，覆盖 OOMKilled、ImagePullBackOff、FailedScheduling、CrashLoop、ConfigError、PVC 挂载失败、健康状态与主动弃答场景：

| 指标 | 基线结果 |
|---|---:|
| Root Cause Accuracy | 63.3% |
| Schema Valid Rate | 100% |
| 有效运行 | 12 Cases × 5 Runs |

运行评测：

```powershell
python -m eval run --suite phase1 --runs 5 --profile candidate --trace-dir ./eval-trace
```

对比基线与候选版本：

```powershell
python -m eval compare --baseline <baseline-run> --candidate <candidate-run> --reports-dir reports
```

评测同时记录 Root Cause Accuracy、Wrong Root Cause Rate、Abstention Accuracy、Evidence Recall、Tool Calls、Token Usage 和 Diagnosis Duration。完整说明见 [Phase 2 评测文档](docs/phase2-eval.md)。

## 路线图

每个 Phase 都保持可独立运行，后续能力只增强已有诊断路径，不取代 Kubernetes-only 基线。

| Phase | 能力 | 状态 |
|---|---|---|
| Phase 1 | Headlamp 单集群人工诊断 | ✅ 已完成 |
| Phase 2 | Agent 评测、基线与回归闭环 | ✅ 已完成 |
| Phase 3 | Prometheus/Loki、持久化历史、多资源入口 | ✅ 已实现 |
| Phase 4 | Runbook RAG 与历史 Incident 检索 | 🟡 已实现，测试中 |
| Phase 5 | Alertmanager 告警自动诊断 | 🗺️ 规划中 |
| Phase 6 | 只读修复计划与人工审批 | 🗺️ 规划中 |
| Phase 7 | Policy + Executor 受控执行与审计 | 🗺️ 规划中 |

## 安全与设计边界

- Connector 使用独立 ServiceAccount 和只读 RBAC，不保存 LLM API Key。
- Agent 不直接访问 Kubernetes API，也不持有 kubeconfig。
- 当前主路径只诊断并生成建议，不直接修改集群资源。
- 发送给外部 LLM 的内容可能包含资源名称、标签、Events 和日志片段；请根据环境要求选择模型端点并做好数据治理。
- 当前知识库面向单用户开发环境，多用户 ACL 隔离尚未实现，不应直接作为生产级权限边界。
- 自动修复只有在独立 Executor、策略校验、人工审批、幂等和审计链路完整后才会开放。

## 开发与验证

```powershell
# Connector
cd connector
go test ./...

# Agent Service
cd ../agent-service
.\.venv\Scripts\python -m pytest -q

# Eval Harness（在项目根目录执行）
cd ..
python -m pytest eval/tests -q

# Headlamp 插件
cd headlamp-plugin/ai-diagnosis-plugin
npx tsc --noEmit
npx eslint --cache -c package.json --max-warnings 0 --ext .js,.ts,.tsx src/
npm run build
```

## 项目文档

| 文档 | 内容 |
|---|---|
| [Phase 1 E2E](docs/phase1-e2e.md) | 部署、故障注入和 Headlamp 端到端验收 |
| [Phase 2 Eval](docs/phase2-eval.md) | Case、Runner、评分、基线与候选版本对照 |
| [Phase 3 Deploy](docs/phase3-deploy.md) | Prometheus、Loki、持久化历史与降级验证 |
| [Phase 4 Knowledge](docs/phase4-knowledge.md) | 知识摄取、历史 Incident、引用约束与消融实验 |
| [Model Benchmark](docs/model-benchmark.md) | 评分口径 v3、模型 Profile、多模型评测与报告 |

## 模型对比（2026-09-15）

运行条件：CommandCode Provider API（OpenAI-compatible `/chat/completions`）；phase1 全部 **12 个 Case × 8 个模型 × 1 次**（seed=42，共 96 次尝试）；同一 Agent/工具/Prompt/预算；`scorer_version=3`；隔离命名空间逐次注入→就绪→诊断→清理。原始报告：`reports/benchmark-20260915T134254-a3bdbb/`（`model-benchmark.json` / `attempts.jsonl` / `model-benchmark.md`；含全部 9 模型的原始件 `model-benchmark.all-models.*`）。

> 当前 token 额度有限，仅跑一次；后续额度充裕再测多轮。

> **已排除模型**：`MiniMaxAI/MiniMax-M2.7` —— 在本 key 下经 `/chat/completions` 返回 `400 No available providers match the 'only' filter…`，12/12 失败且无响应身份，不具备可比性，已从对比与下述指标中移除。排除后本批 `comparable=true`。仍存在的单次失败（`Kimi-K2.5` 4×404、若干 180s 诊断超时、1×connector 抖动）按评分口径计入 `system_failed`，不计入根因准确率分母。

模型（profile → 远程模型 ID）：`reference → deepseek/deepseek-v4.1-flash`、`minimax-m2.5 → MiniMaxAI/MiniMax-M2.5`、`kimi-k2.5 → moonshotai/Kimi-K2.5`、`kimi-k2.6 → moonshotai/Kimi-K2.6`、`glm-5.1 → zai-org/GLM-5.1`、`glm-5.2 → zai-org/GLM-5.2`、`qwen3.6-plus → Qwen/Qwen3.6-Plus`、`qwen3.7-plus → Qwen/Qwen3.7-Plus`。

### 一、诊断质量

| 模型 | 端到端成功率 | 根因准确率 | 错误根因率 | 条件错误率 | 弃答成功率 | 可回答覆盖率 |
|---|---|---|---|---|---|---|
| reference | 91.7% | 90.9% | 0.0% | 0.0% | 100.0% | 90.9% |
| minimax-m2.5 | 75.0% | 81.8% | 16.7% | 18.2% | 0.0% | 90.9% |
| kimi-k2.5 | 58.3% | 63.6% | 8.3% | 12.5% | 0.0% | 63.6% |
| kimi-k2.6 | 83.3% | 90.9% | 8.3% | 9.1% | 0.0% | 100.0% |
| glm-5.1 | 83.3% | 90.9% | 0.0% | 0.0% | 0.0% | 90.9% |
| glm-5.2 | 100.0% | 100.0% | 0.0% | 0.0% | 100.0% | 100.0% |
| qwen3.6-plus | 83.3% | 90.9% | 8.3% | 9.1% | 0.0% | 100.0% |
| qwen3.7-plus | 75.0% | 81.8% | 16.7% | 18.2% | 0.0% | 90.9% |

指标说明：

| 指标 | 它回答什么问题 | 怎么算 |
|---|---|---|
| 端到端成功率 | 一次真实诊断最终成功的概率？ | 正确完成次数 ÷ 环境正常的诊断次数 |
| 根因准确率 | 有明确根因的问题能答对多少？ | 正确根因数 ÷ 应该有根因的 Case 数 |
| 错误根因率 | 实际使用时误诊概率多高？ | 错误明确根因数 ÷ 所有正常诊断次数 |
| 条件错误率 | 模型一旦下结论，出错概率多高？ | 错误明确根因数 ÷ 所有明确给根因的次数 |
| 弃答成功率 | 证据不足时会不会正确说不知道？ | 正确弃答数 ÷ 应该弃答的 Case 数 |
| 可回答覆盖率 | 能诊断的问题它实际解决了多少？ | 给出明确结论数 ÷ 应该可回答的 Case 数 |

### 二、证据质量

| 模型 | 必需证据召回率 | 额外有效证据率 | 无支撑证据率 |
|---|---|---|---|
| reference | 30.0% | 95.6% | 0.0% |
| minimax-m2.5 | 15.0% | 96.3% | 0.0% |
| kimi-k2.5 | 14.3% | 97.4% | 0.0% |
| kimi-k2.6 | 36.4% | 90.9% | 0.0% |
| glm-5.1 | 40.0% | 92.3% | 0.0% |
| glm-5.2 | 36.4% | 94.4% | 0.0% |
| qwen3.6-plus | 27.3% | 94.4% | 0.0% |
| qwen3.7-plus | 20.0% | 96.9% | 0.0% |

指标说明：

| 指标 | 它回答什么问题 | 怎么算 |
|---|---|---|
| 必需证据召回率 | 必须找到的证据找全了吗？ | 命中的必需证据 ÷ 所有必需证据 |
| 额外有效证据率 | 除了必需证据，还给没给可追溯的额外佐证？ | 不匹配必需约束、但带可追溯标识（有 source 且 value）的去重证据条目 ÷ 所有去重证据条目 |
| 无支撑证据率 | 有没有给不出、无法追溯来源的证据？ | 缺可追溯标识（无 source 或 value）的去重证据条目 ÷ 所有去重证据条目 |

> 边界：这两项是**可追溯性代理口径**，只判断「能否定位到来源」，不判断内容真假（本阶段不做真实性/虚构判定）。

### 三、成本和稳定性

| 模型 | Token p50 / p95 | LLM 调用次数 | Tool 调用次数 | LLM 延迟 p50 / p95 (ms) | 系统失败率 | 结构化合法率 |
|---|---|---|---|---|---|---|
| reference | 22591 / 25538 | 4.18 | 4.64 | 21006 / 60740 | 8.3% | 91.7% |
| minimax-m2.5 | 20438 / 39980 | 5.55 | 5.36 | 49118 / 87443 | 8.3% | 91.7% |
| kimi-k2.5 | 15566 / 27500 | 5.50 | 2.13 | 21224 / 33823 | 33.3% | 66.7% |
| kimi-k2.6 | 12814 / 35784 | 4.55 | 4.36 | 25124 / 113093 | 8.3% | 91.7% |
| glm-5.1 | 12395 / 20712 | 3.90 | 3.60 | 53902 / 68420 | 16.7% | 83.3% |
| glm-5.2 | 17634 / 50693 | 4.42 | 4.00 | 42121 / 129216 | 0.0% | 100.0% |
| qwen3.6-plus | 28386 / 58558 | 5.73 | 5.27 | 48631 / 73933 | 8.3% | 91.7% |
| qwen3.7-plus | 18125 / 30437 | 3.91 | 4.09 | 30091 / 84582 | 8.3% | 91.7% |

指标说明：

| 指标 | 它回答什么问题 | 怎么算 |
|---|---|---|
| Token p50 / p95 | 一次诊断大概消耗多少 Token？ | 每次诊断 `prompt_tokens+completion_tokens` 合计的 p50 / p95 |
| LLM 调用次数 | 一次诊断平均要问模型几次？ | LLM 调用总次数 ÷ 正常诊断次数 |
| Tool 调用次数 | 一次诊断平均要查几次集群？ | Tool 调用总次数 ÷ 正常诊断次数 |
| LLM 延迟 p50 / p95 | 模型本身回一次有多慢？ | 每次 LLM 调用耗时的 p50 / p95（不含注入/清理） |
| 系统失败率 | 有多少次没能得出结果（超时/上游报错）？ | `system_failed` 次数 ÷ 环境正常的诊断次数 |
| 结构化合法率 | 输出能不能被程序稳定处理？ | Schema 合法次数 ÷ 所有尝试 |

说明：
- 分母为 0 的指标显示 `-`；费用、缓存 Token、推理 Token 本 Provider 未提供可靠数据，均为 `null`，不写 0。
- `弃答成功率 0.0%` 表示该模型的应弃答 Case 未正确弃答或该次诊断失败（`system_failed` 计入该分母）。
- 逐 Case、逐次尝试明细见 `reports/benchmark-20260915T134254-a3bdbb/`；可比性判定见 `model-benchmark.json` 的 `comparable` / `excluded_models`。

## 致谢

k8sPilot 构建于 [Headlamp](https://github.com/kubernetes-sigs/headlamp) 的插件能力之上，并使用 Kubernetes、Prometheus、Loki 与 Fluent Bit 提供实时诊断事实。
