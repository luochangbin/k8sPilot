# Phase 2：Agent 评测与能力改进

Phase 2 不扩展生产诊断能力，而是把 Phase 1 的 Kubernetes-only Agent 变成**可重复、可比较、可回归**的工程事实：版本化故障 Case → 注入 → 诊断 → Trace → 确定性评分 → 冻结基线 → 候选版本成对对照。

## 新增组件

| 组件 | 说明 |
|---|---|
| `agent-service` | 契约对齐（移除 `cluster_id`）+ 可评分字段（`root_cause_code`、结构化证据）+ 轻量 JSONL Trace |
| `eval/` | Eval Harness：Case Loader、Fault Injector、Runner、Scorer、Reporter、Compare CLI |
| `eval/cases/` | 首批 Case（OOM、ImagePull、调度失败、CrashLoop、配置错误、FailedMount、健康、证据不足拒答）；`eval/cases/versions/` 存放**按版本存档**的历史定义 |
| `eval/suites/phase1.yaml` | Kubernetes-only Benchmark 套件 |

## 运行前准备

1. 真实单集群可达（`kubectl` 可用），并已部署只读 Connector 与 Agent Service。
2. Agent Service 启动时设置 `TRACE_DIR`，让 Eval Runner 能按 `diagnosis_id` 收集 Trace：
   ```powershell
   set TRACE_DIR=D:\AI\k8sPilot\eval-reports\trace
   ```
3. 保持 Agent Service 运行（默认 `http://localhost:8001`）。

## 冻结基线（`phase1`）

> `phase1.yaml` 的条目现在写成 `case-id@case_version`，解析到 `eval/cases/versions/<id>.v<version>.yaml`
> 的**存档定义**，因此重跑 `--suite phase1` 复现的是当年那份 Ground Truth（例如
> `pod-crashloop-001@1` 仍然是 `CRASH_LOOP_BACKOFF`，而当前工作区里的
> `pod-crashloop-001.yaml` 已升为 `@2` / `APPLICATION_EXIT_NONZERO`）。
>
> 注意两点限制：
> 1. 历史 **报告** 是用当时的 `scorer_version`（v3）与 prompt/tool hash 产生的；`eval compare`
>    会因 scorer 版本不同直接判 `comparable=false`（现在还有 case 集合与根因词表版本校验）。
>    要对比旧结果，需用当前 scorer 从原始 `case-results.jsonl` 重新打分。
> 2. 当前的原因级评测请用 `--suite cause-level`（15 个用例，覆盖 Pod/Deployment/Node/PVC），
>    它与 `phase1` 不可直接比较。

```powershell
cd D:\AI\k8sPilot
python -m eval run --suite phase1 --runs 5 --profile baseline --trace-dir D:\AI\k8sPilot\eval-reports\trace
```

结果落在 `reports/baseline-<ts>/`（`run.json` 里记录 `cases: [id@version]`、
`scorer_version` 与 `root_cause_vocabulary_version`）。

## 候选版本对照

对一次单变量改动（Prompt、模型、Tool Schema、裁剪规则）跑同套件：
```powershell
python -m eval run --suite phase1 --runs 5 --profile candidate --trace-dir ...\trace
python -m eval compare --baseline baseline-<ts> --candidate candidate-<ts> --reports-dir reports
```

对比报告覆盖：根因准确率、错误根因率、证据召回、耗时/Token P50/P95，并给出门槛判定（关键 Case 无回归、Wrong Root Cause Rate 不上升、结构化输出合法率不下降）。

## 复用 Phase 2 基准做 Phase 3 消融

Phase 3 引入 Prometheus/Loki 时，用同一 `--suite phase1` 跑 `K8s`、`K8s+Prometheus`、`K8s+Loki`、`K8s+Prometheus+Loki` 四组对照，证明每个数据源的净收益。

## 验收标准

1. 一条命令完成 注入→判稳→诊断→Trace→评分→清理→报告，无人工改结果。
2. 每 Case ≥5 次；报告区分 Agent/系统/Fixture 错误，可从 `diagnosis_id` 定位 Tool 路径。
3. 同一份 `case-results.jsonl` 可离线重算出相同 `report.json`。
4. 候选改动生成 Baseline/Candidate 成对报告，列出改善/回归/成本变化。
5. Phase 1 三条 Headlamp 端到端场景（OOM/ImagePull/FailedScheduling）继续通过——Eval Report 不能替代产品链路验收。

## 已知边界

- Trace 为轻量 JSONL，未部署 OTLP Collector；Phase 2 不以完整观测平台为验收前提。
- Scorer 纯确定性，不引入 LLM 盲审；同义根因覆盖问题留给独立盲审维度。
- `kubectl` 缺失或集群不可达时，Case 记为 `fixture_failed`（不计入准确率）。
