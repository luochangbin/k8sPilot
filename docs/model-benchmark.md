# 多模型横向评测（Model Benchmark）

依据：`docs/model-benchmark-handoff.md`（验收阶段 A–D）。本文记录已实现能力、执行方式、比较限制与验证状态。

## 1. 范围

- 在既有 Agent + Eval 上增加**可复现的多模型评测**，用真实数据比较适合 Kubernetes 诊断的模型。
- 本阶段**仅 OpenAI-compatible `/chat/completions`**；Claude 系模型走 `/messages`，不在范围内。
- 保留 Headlamp → Agent Service → 只读 Connector 链路；保留实时证据优先、知识仅辅助、证据不足可弃答的边界。
- 不做模型路由、自动修复、多集群、多租户、本地 GPU 性能评测。

## 2. 评分口径 v3（scorer_version = "3"）

结果分类保留 `fixture_failed / system_failed / schema_failed / diagnosis_correct / diagnosis_incorrect`，并明确：

- `fixture_failed`：环境未就绪，**不计入诊断质量分母**，单独报告数量与比例。
- `system_failed` / `schema_failed`：计入「故障就绪后的端到端失败」，不能被过滤掉以提高成功率。
- 明确根因：`root_cause_code` 或 `root_cause` 非空；是否正确按版本化 Ground Truth 判断。
- 有效弃答：`insufficient_evidence=true` 且两个根因字段均为空。**字段冲突**（有明确根因却标弃答）为无效输出，不算正确弃答，且该明确结论计入错误根因。
- 预期弃答却给出明确根因：计为错误根因，同时记录弃答失败（风险标记与失败分类并存）。
- 健康 Case 由 Ground Truth 表达，不推断「健康=弃答」。

报告指标（均为「分子/分母」，分母为 0 返回 null）：

| 指标 | 定义 |
|---|---|
| end_to_end_correct_rate | 正确完成 / fixture 成功（含正确弃答与健康） |
| root_cause_accuracy | 正确根因 / fixture 成功且预期可回答 |
| wrong_root_cause_rate | 错误明确根因 / fixture 成功 |
| conditional_wrong_root_cause_rate | 错误明确根因 / 输出明确根因 |
| abstention_recall | 正确弃答 / fixture 成功且预期弃答 |
| answerable_coverage | 可回答中输出明确结论 / fixture 成功且预期可回答 |

证据指标：`required_evidence_match_ratio`（匹配必需证据的去重输出条目 / 全部去重输出证据条目）取代旧的 `evidence_precision`；去重键为 `resource_uid+source+path+operator+value`，缺失字段不补。必需证据之外的条目**不**自动判为虚构。`evidence_recall` 仍为必需证据覆盖比例。

**矛盾输出为无效**：`insufficient_evidence=true` 同时给出明确根因属无效输出（`invalid_output=true`）；即使 `root_cause_code` 命中答案也**不计**为正确诊断，同时保留明确根因这一风险标记（可回答 Case 记 `diagnosis_incorrect` 且 `root_cause_correct=false`；预期弃答 Case 另记 `wrong_root_cause=true`）。

**未知用量不写零**：无 Trace 或 LLM 未回报 Token 时，`token_usage=null`、`token_usage_complete=false`；报告聚合只统计已回报的样本，并给出 `token_usage_known_runs` / `token_usage_missing_runs`。

**请求尝试数如实记录**：LLM 客户端回传实际请求尝试数（含内部重试）；Trace `llm.call` 记录 `attempts`/`retries`，benchmark 的 `llm_request_attempts` 为各次调用实际尝试数之和，失败时使用该 profile 的 `max_retries` 而非全局配置。

**新旧不可直接比较**：报告带 `scorer_version`；`eval compare` 在两个 run 的 `scorer_version` 不一致时不产生 delta 并标记 `comparable=false`（旧报告无该字段，视为不可比）。

## 3. Model Profile 与调用隔离

- 配置：可选 `LLM_PROFILES_FILE`（YAML，相对路径以 `agent-service/` 根解析）；未配置时把既有 `LLM_*` 映射为 `default` profile，行为不变。示例见 `agent-service/llm-profiles.example.yaml`。
- 只允许**服务端预配置** profile；请求只能传 profile 名，不能传 URL/密钥/Header。配置只保存密钥的环境变量名（如 `COMMANDCODE_API_KEY`）。
- 参数白名单：`max_tokens`、`temperature`；未知参数、重复键、未知 protocol、缺失密钥均报清晰错误，不静默回退。
- 请求字段：`DiagnosisRequest.model_profile`（可选）。`ENABLE_MODEL_PROFILE_SELECTION=false`（默认）时显式传 profile 返回 **403**；未知 profile 在创建 Session 前返回 **422**。
- 隔离：创建诊断时固定一份**不可变** `ExecutionContext`（含已解析模型与客户端），后台线程使用它，不修改共享 Agent/Config/客户端。并发诊断 A/B 各自使用自己的模型。
- 可追溯：服务端将 requested/response model 与生效参数写入 Trace（`resolved_profile / provider / protocol / requested_model_id / effective_parameters / config_fingerprint`；`llm.call` span 带 `response_model_id`），创建响应也回带 `model` 元数据；**不记录密钥与 Header 值**。

## 4. 执行方式

```powershell
# 1) 启动 Agent，启用 profile 选择并使用 profiles 文件
#    .env: LLM_PROFILES_FILE=llm-profiles.yaml / ENABLE_MODEL_PROFILE_SELECTION=true
#          COMMANDCODE_API_KEY=...
# 2) 多模型评测
python -m eval benchmark --suite phase1 --models reference,candidate-a,candidate-b `
  --runs 3 --seed 42 --max-diagnoses 72 --agent-url http://localhost:8000 `
  --trace-dir D:\AI\k8sPilot\eval-trace --reports-dir reports
```

- `benchmark_id`：批次唯一（时间戳 + 随机后缀），不只依赖秒级时间戳。
- `attempt_id`：每 Case × Model × repetition 唯一；运行计划先写盘（`model-benchmark.plan.json`），含执行顺序与 seed。
- 首版串行；在每个 Case/repetition 分块内以固定 seed 打乱模型顺序。
- 每次尝试独立：注入 → 就绪 → 诊断 → 收集 → 清理。**清理失败立即停止后续注入**，防止污染结果。
- 不自动重跑失败的诊断；LLM 请求级重试由客户端单层控制并有界。
- `--max-diagnoses` 为硬上限，`--time-limit-seconds` 为批次时间上限；达限停止启动新尝试并保留 partial 报告与 `stop_reason`。
- 失败尝试逐条记录，不以缺失行掩盖。
- 旧 `run --model` 仅作**声明标签**，不证明实际模型；单模型选择用 `run --model-profile`。

输出：`reports/<benchmark_id>/` 下 `model-benchmark.plan.json`、`attempts.jsonl`、`model-benchmark.json`、`model-benchmark.md`。

## 5. 记录与解释限制

- 每条尝试记录身份与条件（attempt/model/execution_order/seed、requested/response model、生效参数、prompt/tool/k8s/scorer 指纹、时间、工具与 LLM 次数、Token、status/failure_category/score 等）。缺失值写 null/unknown，**不写 0**；`usage_complete` 标记完整性。
- `reasoning_tokens`/`cached_tokens` 不可得时为 null，且不与 `completion_tokens` 重复相加。
- Token ≠ 成本：无可靠费率时只报用量；`cost_estimate=null`、`pricing_source=unknown`，套餐额度/估算/账单分开表达。
- 端到端耗时（含注入/清理）与 LLM/Tool 耗时分别记录，不宣称求和等于墙钟时间。
- 只在**相同 Case 集合**上比较，困难子集单独展示；样本少时标明数量与波动，不从差值断言稳定优胜。
- `comparable` 由**实际证据**计算而非固定 True：要求每个产生结论的模型都有服务端确认的身份（`resolved_profile`、`requested_model_id`、`config_fingerprint`）且 `requested==response`；缺失身份、ID 不一致、provider/protocol 或知识开关跨尝试不一致时标记 `comparable=false` 并给出 `incomparable_reason`，不按请求标签假装可比。
- 不生成主观加权总分或自动推荐；模型 ID/Provider 变化为比较维度，其他条件变化需显式标记，不静默并入同一排名。

## 6. 费用与失败解释

- 401/403（权限/额度）、429（限流）、400（模型不支持/走错端点）、5xx 通过 `failure_category` 与错误文本体现在 attempts 与报告中。
- 本阶段为**实时 Agent Benchmark**：固定故障/就绪/工具权限/Prompt/知识开关/预算，允许模型自主选择调查路径，不宣称最终上下文完全相同。

## 7. 验证记录

### 已实现并通过离线测试

- 评分口径 v3（`eval` 31 tests，含冲突弃答、严格 operator/resource_uid 匹配、额外/重复证据、零分母、不可比标记）。
- Profile 解析与错误、每诊断隔离与 403/422（`agent-service` 50 tests）。
- benchmark 计划/预算/失败记录（`eval/tests/test_benchmark.py`）。
- 命令：`agent-service/.venv/Scripts/python.exe -m pytest -q`（53 passed）、`... -m pytest eval/tests -q`（28 passed）、`git diff --check` 通过。

### 真实闭环（阶段 D，已完成）

条件：CommandCode Provider API（OpenAI-compatible `/chat/completions`），单 key；3 个 profile：
`reference = gpt-5.6-sol`、`candidate-a = deepseek/deepseek-v4.1-flash`、`candidate-b = zai-org/GLM-5.3`。
测试集群 kind-eval-cluster（隔离命名空间 `eval-pod-*`，逐次注入→就绪→诊断→清理）。

- 工具协议冒烟（`reports/benchmark-20260915T082959-367ea2`）：3 模型 × `pod-healthy-001` 全部 `diagnosis_correct`，`requested==response` 模型 ID 一致，工具调用与 Token 正常。
- 闭环评测（`reports/benchmark-20260915T083937-7d68d9`，6 attempts，`stop_reason=null`）：

| 模型 | 尝试 | verdict | pod-oomkilled-001 | pod-abstain-restart-001 | 调用身份 |
|---|---|---|---|---|---|
| reference (gpt-5.6-sol) | 2 | correct×1, fixture_failed×1 | correct | **fixture_failed**（环境未就绪，不计入分母） | id 一致 |
| candidate-a (deepseek-v4.1-flash) | 2 | correct×1, incorrect×1 | correct | incorrect（给出明确根因，**弃答失败**，wrong_root_cause 计 1） | id 一致 |
| candidate-b (GLM-5.3) | 2 | correct×2 | correct | correct（正确弃答，abstention_recall=1.0） | id 一致 |

满足阶段 D 要求：**两个实际模型（candidate-a、candidate-b）各自完成了一个明确根因 Case 与一个应弃答 Case**；调用身份（requested/response 模型 ID）、原始结果、Trace（`eval-trace/diag_*.jsonl`）与清理均留存。样本 n=1，仅作方向性记录，不据此断言模型优劣。

已知局限记录：`reference` 在应弃答 Case 上出现一次 `fixture_failed`（namespace/就绪未成功，非模型问题，已被正确排除在质量分母外）；为减少此类偶发，`eval/injector.py` 的清理删除超时从默认 60s 提升到 180s（仍为前台等待、失败即停止的语义不变）。

### 审阅修正（2026-09-15）

针对外部审阅的 6 个问题已核实并修复（均属实）：

1. **可比性不再固定 True**：`benchmark.py` 现依据实际身份/指纹/条件计算 `comparable`，缺失或 `requested!=response` 时输出 `incomparable_reason`，并按服务端确认的 `resolved_profile` 校验而非请求标签。
2. **矛盾输出不再计正确**：`scorer.py` 对 `insufficient_evidence=true` 且有明确根因的输出记 `invalid_output=true`，可回答 Case 也不计 `diagnosis_correct`，同时保留风险标记。
3. **未知 Token 不再显示为零**：`summarize_trace` 无 Trace/无用量时返回 `token_usage=null`，报告只聚合已知样本并给出覆盖率。（同时修正了 `effective_knowledge_flags` 把 incidents 误记为 `enable_knowledge` 的笔误。）
4. **重试次数如实记录**：`llm.py` 回传实际尝试数（`LLMError.attempts`、`last_attempt_count`），`agent.py` 按实际值写 Trace；`benchmark` 的 `llm_request_attempts` 改为累加实际尝试数。
5. **证据约束字段必须存在且一致**：`scorer._matches_required` 对 Ground Truth 指定的 `operator`（以及新增的 `resource_uid`）要求输出证据**存在且完全相等**；输出缺失 operator 不再算命中。`EvidenceRequirement` 新增可选 `resource_uid`，仅当 GT 指定时才强制。
6. **响应模型身份缺失即不可比**：`benchmark.py` 要求 Provider 返回 `response_model_id`；仅请求了模型但无响应身份时记 `missing_response_identity`、`identity_ok=false`、`comparable=false`，不再凭请求标签判定可比。

因评分语义在本轮发生变更，`scorer_version` 由 `2` 提升为 `3`：v2/v3 结果不可直接作差（`compare` 会标记不可比）。

测试：`agent-service` 53 passed、`eval` 31 passed（含新增回归）。真机验证：`reports/benchmark-20260915T102053-762ada`（1 模型 × healthy，n=1）→ `comparable=true`、`identity_ok=true`、`llm_request_attempts=3`、`token_usage` 已知且 `token_usage_complete=true`。

**重要**：先前的 3 模型闭环报告 `reports/benchmark-20260915T083937-7d68d9` 产生于上述修正之前（且为 `scorer_version=2`），其可比性、重试计数与 Token 聚合口径已过时，**不得用于正式模型选型**；正式比较需用修正后的代码重新执行并统一为 `scorer_version=3`。

### 可比性修正（2026-09-16，第 3 轮）

针对报告可比性的 3 个 P1 问题已核实并修复（均属实）：

1. **部分身份未知仍参与比较**：改为**逐条校验**——任何真正调用过模型（`llm_request_attempts>0` 或已有评分结论）但身份未验证的尝试都会写入 `incomparable_reason`；不再用 `any(identity_ok)` 以一条已验证记录替整个模型背书。
2. **无共同 Case 仍称可比 / 比较范围不一致**：覆盖集合按**已执行尝试**定义（超时/失败也算跑过）。共同 Case 为空 → `no_common_cases`；各模型 Case 集合不一致 → `case_sets_differ`；有结果的模型少于两个 → `insufficient_models_with_results`。**聚合只在共同 Case 集合上进行**（新增 `compared_attempt_count`），单模型的额外 Case 不再稀释或垫高比较。
3. **同 profile 配置漂移未检测**：同一 profile 内出现多个 `config_fingerprint` 或不同 `effective_parameters` 时标记 `config_fingerprint_varies` / `parameters_vary`，不静默合并。

### 可比性边界修正（2026-09-16，第 4 轮）

再核实 2 个 P2 边界问题（均属实）并修复：

1. **部分模型无结果仍称可比**：若请求比较的模型中有的**完全没有尝试**（无任何执行记录），记为 `model_missing_results:<names>` 使整批 `comparable=false`，并在报告中输出 `models_requested` / `models_compared` / `models_missing`（不再静默只比较有结果的子集）。
2. **失败尝试的配置漂移未检查**：配置一致性检查从“仅已评分且身份已验证”扩展为“**所有有执行身份的尝试（含 system_failed 等失败）**”，因为失败次数同样进入质量统计分母；同一 profile 内成功与失败使用不同指纹/参数时标记漂移。

按新规则重算：8 模型真实报告仍 `comparable=true`（`models_missing=[]`、共同 Case=12），指标不变。测试：`eval` **39 passed**。

按新规则重算：8 模型真实报告仍为 `comparable=true`（共同 Case=12，各模型 `compared_attempt_count=12`），README 中的质量指标不变；单模型批次现因 `insufficient_models_with_results` 标为不可比（无对照对象，属预期，故上条 `benchmark-20260915T102053-762ada` 的 `comparable=true` 按新规则已过时）。测试：`eval` **37 passed**。

### 8 模型全量对比（2026-09-15）

- 条件：phase1 全 12 Case × 8 模型 × 1 次（seed=42，`scorer_version=3`），96 次尝试；报告 `reports/benchmark-20260915T134254-a3bdbb/`（`model-benchmark.json` / `attempts.jsonl` / `model-benchmark.md`；9 模型原始件保留为 `model-benchmark.all-models.*`）。模型：`deepseek/deepseek-v4.1-flash`（参考）、`MiniMaxAI/MiniMax-M2.5`、`moonshotai/Kimi-K2.5`、`moonshotai/Kimi-K2.6`、`zai-org/GLM-5.1`、`zai-org/GLM-5.2`、`Qwen/Qwen3.6-Plus`、`Qwen/Qwen3.7-Plus`。
- **排除模型**：`MiniMaxAI/MiniMax-M2.7` 在本 key 下经 `/chat/completions` 返回 `400 No available providers match the 'only' filter…`，12/12 失败且无响应身份，已排除（报告记录 `excluded_models` / `exclusion_reason`）。排除后 `comparable=true`。
- 结果（概率以 % 表示）：`GLM-5.2` 端到端正确率 100.0%、根因准确率 100.0%、弃答召回 100.0%；`reference`/`Kimi-K2.6`/`GLM-5.1`/`Qwen3.6-Plus` 根因准确率 90.9%；`MiniMax-M2.5`/`Qwen3.7-Plus` 81.8%；`Kimi-K2.5` 63.6%。完整质量与成本表见 `README.md`「模型对比」。
- 仍存在的单次失败（`Kimi-K2.5` 4×404、若干 180s 诊断超时、1×connector 抖动）按评分口径计入 `system_failed`，不计入根因准确率分母；样本 n=1/Case，结论为方向性参考。
- 报告渲染（`model-benchmark.md`、`report.md`）现统一将概率输出为百分比（JSON 仍保留 0–1 原始比率）。
- 各指标定义、分母口径与失败分类见 `README.md`「模型对比 › 指标说明」。
- 证据质量新增两项**可追溯性代理口径**：`evidence_extra_rate`（不匹配必需约束但可追溯的去重证据条目占比，即“额外有效证据率”）与 `evidence_unsupported_rate`（缺 source/value 的去重证据条目占比，即“无支撑证据率”）；二者不重叠，且**不代表内容真假**（本阶段不做真实性/虚构判定）。成本项新增 `llm_duration_ms` p50/p95 聚合。
- 上述证据细分与 LLM 延迟已写入评分/报告代码；本次报告由存量 `diagnoses.db` 原始结果按同一口径重算（`rescore_note`），原始 9 模型件保留为 `model-benchmark.all-models.*`。

### 尚未验证 / 风险

- 首轮仅 6 次尝试、每模型每 Case n=1；正式排名需要更多重复与更多 Case，且只在相同 Case 集合上比较。
- Token 成本未接入费率（`cost_estimate=null`、`pricing_source=unknown`），未做费用对比。
- Claude 系模型未测（需 `/messages`，超出本阶段范围）。

## 8. 交付清单对照（交接 §10）

1. 小范围实现及测试：✅（A/B/C 离线；保留单模型与知识消融能力）
2. 无真实凭据的 Profile 示例与 `.env.template` 说明：✅（`llm-profiles.example.yaml`、`agent-service/.env.template`）
3. benchmark CLI、评分版本、结果 Schema 与 JSON/Markdown 报告：✅
4. 执行方法、比较限制、费用与失败解释：✅（本文第 2–6 节）
5. 验证记录（已实现/离线已验证/真实已验证/未验证及原因）：✅（第 7 节）
6. 差异清单及已知限制，未把待实现能力标成完成：✅（本文与交接文档一致）
