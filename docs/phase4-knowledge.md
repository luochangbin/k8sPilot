# Phase 4：知识增强与历史经验检索（RAG）

状态：**暂不实现（缺乏知识库）**。当前没有可用的真实运维知识库，交付范围不含本阶段能力；`agent-service/app/knowledge` 与 `eval` 中的实现与消融实验仅作机制记录，默认关闭（`KNOWLEDGE_DB` 未设置即不启用检索）。

目标：在 Phase 3 的实时诊断之上叠加「文档知识 + 已验证历史 Incident」两类参考信息，帮助 Agent 在知识缺口处选择下一步实时 Tool、解释现象、生成建议。参考信息**永不**充当 Root Cause 证据——实时 Evidence 优先级恒高于任何检索结果。

## 范围与实现

| 项 | 说明 |
|---|---|
| 数据 | SQLite fts5 本地知识库：6 篇文档（runbook/known_issue）+ 5 条已验证 Incident（`eval-trace/knowledge.db`） |
| 模块 | `agent-service/app/knowledge/`：models / store / service / ingest / seeds |
| 摄取 | `python -m app.knowledge.ingest --db <path>`（种子已落盘） |
| 检索 | `search_knowledge`（文档 chunk）与 `search_incidents`（已验证 Incident），强类型、限量、带引用 |
| 触发 | Agent 仅在实时事实已收集、存在知识缺口时才调用；同一问题不重复检索 |
| 输出 | `DiagnosisResult.historical_cases[]` / `knowledge_references[]`，每条带 `used_for`（hypothesis/investigation/explanation/recommendation），绝不打 `root_cause_evidence` 标记 |

## 导入已有知识库

### 当前支持边界

当前 `python -m app.knowledge.ingest` 是面向受控知识的种子摄取工具，只读取 `agent-service/app/knowledge/seeds.py` 中的 `SEED_DOCUMENTS` 与 `SEED_INCIDENTS`。它不会直接扫描 Markdown 目录，也不接受 JSON、YAML、网页地址、向量数据库或任意第三方 SQLite 文件。

已有知识可以通过两种方式接入：

1. **复用兼容数据库**：如果文件由当前版本 k8sPilot 的 `KnowledgeStore` 创建，可直接把 `KNOWLEDGE_DB` 指向该文件，不需要再次执行摄取命令。
2. **迁移现有资料**：把文档和历史故障映射为下述种子模型，再运行内置摄取命令生成或更新 `knowledge.db`。

不要把任意已有 SQLite 文件直接配置为 `KNOWLEDGE_DB`。只有包含当前 `kb_documents`、`kb_chunks`、`incidents` 及其 FTS5 索引结构的 k8sPilot 数据库才兼容。

### 文档资料映射

把 Runbook、SOP、产品文档或 Known Issue 转换为 `KnowledgeDocument`，加入 `SEED_DOCUMENTS`。最小示例：

```python
KnowledgeDocument(
    document_id="kb-payment-db-timeout-001",
    source_type="runbook",
    title="支付服务数据库超时处理手册",
    source_uri="https://kb.example/runbooks/payment-db-timeout",
    product="payment-api",
    versions=["2.4.x"],
    environments=["production", "staging"],
    owner="payment-sre",
    valid_from="2026-01-01T00:00:00+00:00",
    acl_tags=["team:payment", "role:sre"],
    content="""
## 症状
Pod 日志持续出现数据库连接超时。

## 排查
核对连接池配置、数据库 Endpoint 和网络策略。

## 验证
错误率恢复且新建连接成功。
""",
)
```

字段要求：

| 字段 | 规则 |
|---|---|
| `document_id` | 稳定且唯一；再次摄取相同 ID 会更新原文档和分块 |
| `source_type` | 仅使用 `runbook`、`product_doc`、`known_issue`、`sop` |
| `status` | 只有 `active` 文档可被检索；模型默认值即为 `active` |
| `content` | 规范化后的正文；`## ` 标题和空行用于结构化分块 |
| `source_uri` | 原始知识来源，供 UI 引用和人工追溯；内部页面也应填写稳定地址 |
| `versions` / `environments` | 用于按产品版本和环境缩小候选范围 |
| `valid_from` / `valid_until` | ISO 8601 时间；超出有效期的内容不会参与检索 |

### 历史 Incident 映射

只把已经关闭、根因明确且修复结果经过验证的故障加入 `SEED_INCIDENTS`：

```python
IncidentCase(
    incident_id="inc-2026-payment-db-001",
    status="verified",
    product="payment-api",
    product_version="2.4.1",
    environment="production",
    resource_kind="Pod",
    symptoms=["CrashLoopBackOff", "database connection timeout"],
    evidence_signature=[
        {"source": "logs", "key": "error", "value": "connection timeout"}
    ],
    root_cause_code="DATABASE_CONNECTION_POOL_MISCONFIGURED",
    remediation_summary="修正连接池上限并滚动发布。",
    verification={"outcome": "success", "verified_at": "2026-09-01T10:00:00Z"},
    evidence_summary="应用日志连接超时；数据库 Endpoint 正常；连接池上限配置错误。",
)
```

只有 `status="verified"` 的 Incident 可被检索。`incident_id` 必须稳定且唯一；再次摄取相同 ID 会更新原记录。审批拒绝、未验证结论或仍在调查中的 Incident 不应进入该集合。

### 执行摄取并启用

在 `agent-service` 目录执行：

```powershell
$knowledgeDb = Join-Path (Resolve-Path ..) 'data\knowledge.db'
.\.venv\Scripts\python -m app.knowledge.ingest --db $knowledgeDb --show
```

预期输出包含每篇文档的 chunk 数、每条 Incident 的根因编码，以及最终计数，例如：

```text
store ready at D:\AI\k8sPilot\data\knowledge.db
  documents(active)=6 incidents(verified)=5
```

在 `agent-service/.env` 中使用绝对路径，避免 Agent Service 从不同工作目录启动时把相对路径解析到错误位置：

```dotenv
KNOWLEDGE_DB=D:\AI\k8sPilot\data\knowledge.db
```

重启 Agent Service 后配置才会生效。随后对与导入内容匹配的故障发起诊断；验收时同时确认：

- 诊断 Trace 中出现 `search_knowledge` 或 `search_incidents` 调用；
- 结果中的 `knowledge_references[]` 或 `historical_cases[]` 能追溯到导入的 ID；
- 检索内容只用于假设、调查、解释或建议，没有被标成 `root_cause_evidence`；
- 当前实时证据与历史知识冲突时，最终结论仍以实时证据为准。

如果需要直接批量导入 Markdown/JSON/YAML、定期同步 Confluence/Git 或接入向量数据库，需要新增独立 importer；当前版本尚未提供这些入口。

### Agent 集成（请求 → 工具门控）

- `DiagnosisRequest.enable_knowledge` / `enable_incidents`：可选布尔；`None` = 模块可用即默认开启。二者都关闭等价于 Phase 2/3 的 Kubernetes-only（消融 A 组条件）。
- 门控在 Agent 侧 `_retrieval_flags()` 决定工具是否暴露；关闭时 `search_*` 根本不进 Tool Schema，模型无法调用，因此 A/B/C 消融组成立。
- 结果解析在 `_parse_result()`：只接受 `retrieval_id` 命中本轮实际检索的引用，再注入 `used_for`，防止模型编造来源。

### 检索优先级与防误导

提示词约定：实时 Evidence > 历史 Incident > 文档 > 模型自身。示例：历史 Case 指向数据库连接池，但当前 exitCode=137 / reason=OOMKilled / 内存贴近 Limit 时，Agent 必须判定内存超限，历史 Case 只能记为被否定的低优先级假设。冲突 Case 的合规行为已有单测与真机验证覆盖。

## UI：三分区展示

`headlamp-plugin/ai-diagnosis-plugin/src/DiagnosisSection.tsx` 将结果分为**视觉与文案互不混淆**的三块：

1. **实时证据**（左侧主色描边 + 「实时」Chip）——支撑 Root Cause 的当前环境事实。
2. **历史相似案例**（底色块 + 「参考」Chip）——`incident_id`、产品/版本、`root_cause_code`、症状、证据/处置/验证摘要、`used_for`，标注「参考」与「相似经验回顾，不构成实时证据」。
3. **文档知识参考**（底色块 + 「参考」Chip）——文档标题、章节/版本、内容摘要、`used_for`，并给出可追溯的「来源文档」链接（`citation.source_uri`）。

验收方式：`npm run build` 后复制产物到 `%APPDATA%\Headlamp\Config\plugins\ai-diagnosis-plugin\`；本轮已构建并安装（tsc 0 错误、eslint 0 错误、prettier 通过）。真实页面点击验收待用户刷新 Headlamp 后确认。

## Eval：四组消融

`eval` Runner/CLI 增加两个 tri-state 检索开关，每次 run 传一组条件：

```powershell
python -m eval run --suite phase1 --runs N --profile <group> `
  --enable-knowledge {auto,on,off} --enable-incidents {auto,on,off} `
  --trace-dir D:\AI\k8sPilot\eval-trace
```

| 实验 | enable_knowledge | enable_incidents | CLI |
|---|---|---|---|
| A 实时（K8s-only） | off | off | `--enable-knowledge off --enable-incidents off` |
| B +文档 | on | off | `--enable-knowledge on --enable-incidents off` |
| C +Incident | off | on | `--enable-knowledge off --enable-incidents on` |
| D 全开 | on | on | `--enable-knowledge on --enable-incidents on` |

- 改动：`eval/runner.py`（`run()`→`_run_case()`→`_run_diagnosis()` 透传；meta 记录 `enable_knowledge`/`enable_incidents`，payload 仅当非 `None` 才附带该键）、`eval/cli.py`（`_tri_state()` + 两个参数）。
- 对照说明：`reports/baseline-20260902T130459` 为 Phase 2 冻结 K8s 基线（当时无 knowledge 模块，`run.json` 无开关键）。A 组以同代码双关闭复现该条件；但冻结基线 prompt/tool_schema hash 与现代码不同，严格对照应以同代码 A 组为底，基线仅作历史参考。

### 正式消融结果（2026-09-09，全 12 Case × 4 组 × 3 次，n=36/组）

报告目录（A/B/C/D 各 36 次尝试，全部完成、无 fixture/system 失败）：

| 组 | profile | 报告目录 |
|---|---|---|
| A 实时 | ablation-a | `reports/ablation-a-20260909T123057` |
| B +文档 | ablation-b | `reports/ablation-b-20260909T125716` |
| C +Incident | ablation-c | `reports/ablation-c-20260909T132839` |
| D 全开 | ablation-d | `reports/ablation-d-20260909T181505` |

聚合指标：

| 指标 | A | B | C | D |
|---|---|---|---|---|
| Root Cause Accuracy | 0.853 | 0.861 | **0.886** | 0.857 |
| Wrong Root Cause Rate | 0.059 | 0.028 | 0.029 | 0.029 |
| Abstention Accuracy | 0.667 | 1.0 | 1.0 | 1.0 |
| Schema Valid Rate | 0.944 | 1.0 | 0.972 | 0.972 |
| Evidence Recall | 0.613 | 0.606 | **0.734** | 0.656 |
| Evidence Precision | 0.100 | 0.099 | 0.118 | 0.111 |
| diagnosis_incorrect 次数 | 3 | 2 | **1** | 2 |
| 耗时 p50 (ms) / Tool p50 / Token p50 | 19102/4/19907 | 26182/5/21906 | 25066/5/21434 | 24586/5/24065 |

逐 Case Root Cause Accuracy（A/B/C/D，n=3）：

| case | A | B | C | D |
|---|---|---|---|---|
| pod-abstain-restart-001（拒答） | 0.000 | 0.000 | 0.000 | 0.000 |
| pod-configerror-env-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-configerror-mount-001 | 1.000 | 0.667 | 1.000 | 0.667 |
| pod-crashloop-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-failedmount-pvc-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-failedscheduling-node-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-failedscheduling-resource-001 | 0.333 | 0.667 | 0.667 | 0.667 |
| pod-healthy-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-imagepullauth-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-imagepullbackoff-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-oomkilled-001 | 1.000 | 1.000 | 1.000 | 1.000 |
| pod-oomkilled-recovered-001 | 1.000 | 1.000 | 1.000 | 1.000 |

（注：`pod-abstain-restart-001` 为 `abstention_expected` 拒答场景，RCA 列恒 0.000，看 Abstention Accuracy 列。）

检索门控与引用（36 次/组，依据 trace 的 `tool_call` 与 `DiagnosisResult` 引用字段）：

| 组 | search_knowledge 调用 | search_incidents 调用 | kb_refs | inc_refs |
|---|---|---|---|---|
| A | 0 | 0 | 0 | 0 |
| B | 9 | 0 | 16 | 0 |
| C | 0 | 9 | 0 | 10 |
| D | 8 | 7 | 15 | 8 |

**门控证据**：开关值与实际暴露严格一致——A 组（双关）36 次全部零 `search_*`；B 组只发生知识检索、C 组只发生 Incident 检索；引用数等于命中数（无凭空引用）。模型在工具开放时自选是否检索（B/D 中部分 Case 未调用），符合「知识缺口才检索」策略。

**证据完整性**：四组全部 144 个结果中，`evidence[]` 内**无任何**引用来源条目（无 `kb_`/`inc_`/`retrieval` source），检索引用从未冒充实时证据——三类信息隔离与优先级成立。

对照发布门槛：

- **关键 Case 无回归**：oomkilled/oomkilled-recovered/imagepull*/crashloop/failedmount-pvc/failedscheduling-node/configerror-env/healthy 四组 RCA 均 1.0 且稳定；`failedscheduling-resource` 由 A 0.333 升至 B/C/D 0.667（受益于检索）。
- **Wrong Root Cause Rate 不升高**：A=0.059 → B/C/D≈0.028–0.029（下降）。
- **结构化输出合法率不下降**：A schema 0.944（1 例 schema_failed）→ B/C/D ≥0.972。
- **拒答更稳**：abstain-restart 的 Abstention Accuracy A=0.667 → B/C/D=1.0。
- **结论方向**：Incident（C）在本批样本上 RCA 与 Evidence Recall 最优，全开（D）未超过 C。差异来自 n=3/case 与单模型随机性，属**方向性信号而非定论**。

### 早期冒烟（2026-09-09，n=1）

3 个有种子覆盖 Case × 四组，先于正式消融用于确认开关端到端生效：A 双关时零 `search_*`，C/D 同 Case 出现 `search_incidents` 并解析历史 Case；报告见 `reports/ablation-{a,b,c,d}-20260909T075xxx/`。结论与正式组方向一致。

### 运行命令（本轮实际执行）

```powershell
python -m eval run --suite phase1 --runs 3 --profile ablation-a --enable-knowledge off  --enable-incidents off  --trace-dir D:\AI\k8sPilot\eval-trace --model deepseek-v4-flash
python -m eval run --suite phase1 --runs 3 --profile ablation-b --enable-knowledge on   --enable-incidents off  --trace-dir D:\AI\k8sPilot\eval-trace --model deepseek-v4-flash
python -m eval run --suite phase1 --runs 3 --profile ablation-c --enable-knowledge off  --enable-incidents on   --trace-dir D:\AI\k8sPilot\eval-trace --model deepseek-v4-flash
python -m eval run --suite phase1 --runs 3 --profile ablation-d --enable-knowledge on   --enable-incidents on   --trace-dir D:\AI\k8sPilot\eval-trace --model deepseek-v4-flash
```

## 偏离记录

1. **ACL 标签入库但未强制鉴权**：文档/Incident 的 `acl_tags` 已建模入库，但当前 `search_knowledge`/`search_incidents` 未按调用者身份过滤；未授权内容不应进入候选。原因：单集群、单 SRE 用户、无认证层，无「当前用户身份」可绑定。后续引入多用户/鉴权时必须补过滤，否则不得宣称 ACL 隔离生效。
2. **Knowledge 为 agent-service 内模块而非独立服务**：早期方案考虑独立 Knowledge Service；当前按 agent 内模块实现（`app/knowledge/`，进程内调用）。影响：无独立降级/超时边界——`knowledge_degraded`/`no_knowledge_match` 语义由 Agent 侧的「知识缺口才检索 + 检索失败不影响结论」近似覆盖，而非独立服务探活。可观测性（独立 service span、独立 P95）随本偏离受限。
3. **检索失败路径的显式标记未落地**：`knowledge_degraded` / `no_knowledge_match` 尚未作为结构化字段写回 `DiagnosisResult`。已探针验证（2026-09-09）：`KNOWLEDGE_DB` 指向不可开路径（目录）时 app 启动即抛 `sqlite3.OperationalError` 退出（exit 1）——模块**配置可用但运行期 DB 不可开**时属非预期降级（无 `knowledge_degraded` 兜底）；而模块**未配置**（`KNOWLEDGE_DB` 为空）时降级正常（见验证记录 degraded 运行）。
4. **历史 Incident 检索用 keyword（fts5），非向量**：Retrieval 指标（Recall@K、MRR/nDCG）通常采用向量检索口径；当前实现为小型 curated 语料上的关键词检索，直接套用该指标集意义有限，正式评测应说明口径。

## 验证记录

- `agent-service`：`.\.venv\Scripts\python -m pytest -q` → 34 passed（含 knowledge ingest/filter/search/gating/引用解析）。
- `eval`：`python -m pytest eval/tests -q` → 14 passed（新增 3：`_run_diagnosis` 透传开/关、`auto` 省略键、CLI `_tri_state` 解析）。
- **正式消融**（2026-09-09，12 Case × 4 组 × 3 次，n=36/组，全部完成）：见上文聚合/逐 Case/门控统计；报告目录 `reports/ablation-{a,b,c,d}-20260909T12/18xxxx/`。门控与配置一致（A=0 search，B 仅 kb 9/16，C 仅 inc 9/10，D kb8+inc7）；144 结果 evidence 完整性违规 = 0。
- **降级运行**：临时第二实例（端口 8001，`KNOWLEDGE_DB` 置空、独立 DIAGNOSIS_DB/trace）跑 `reports/degraded-20260909T122847/`（configerror-env/oomkilled × 2）：4/4 `diagnosis_correct`，trace 仅实时工具、零 `search_*`。验证后实例已停止。
- **崩溃路径探针（偏离3）**：`KNOWLEDGE_DB` 指向目录启动临时实例（端口 8002）→ `sqlite3.OperationalError: unable to open database file`，进程 exit 1（app 启动即失败，无优雅降级）。
- 引用结构抽查：D 组 `diag_c4a5bc6bc729` 的真实 API 返回与 TS 端接口字段一致（`incident_id/product/.../verification/used_for`）。
- Headlamp 插件：`tsc --noEmit` 0 错误、eslint 0 错误 0 警告、prettier 通过、`npm run build` 成功并复制到 `%APPDATA%\Headlamp\Config\plugins\ai-diagnosis-plugin\`。

**未验证/剩余风险**：Headlamp 页面真实视觉验收未做（需刷新页面人工确认）；正式消融 n=3/case 规模较小，净收益结论仅方向性；偏离 3 的「运行期 DB 不可开」仍为启动即失败、无 `knowledge_degraded` 兜底；模块关闭时的完整历史查询回归未单独跑（degraded 仅覆盖诊断链路）。
