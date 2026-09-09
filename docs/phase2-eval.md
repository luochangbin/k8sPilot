# Phase 2：Agent 评测与能力改进

设计依据：`K8S管理平台智能诊断系统设计.md` §23（Eval Harness）、§32（Trace）、§33（评测原则）。

Phase 2 不扩展生产诊断能力，而是把 Phase 1 的 Kubernetes-only Agent 变成**可重复、可比较、可回归**的工程事实：版本化故障 Case → 注入 → 诊断 → Trace → 确定性评分 → 冻结基线 → 候选版本成对对照。

## 新增组件

| 组件 | 说明 |
|---|---|
| `agent-service` | 契约对齐（移除 `cluster_id`）+ 可评分字段（`root_cause_code`、结构化证据）+ 轻量 JSONL Trace |
| `eval/` | Eval Harness：Case Loader、Fault Injector、Runner、Scorer、Reporter、Compare CLI |
| `eval/cases/` | 12 个首批 Case（OOM、ImagePull、调度失败、CrashLoop、配置错误、FailedMount、健康、证据不足拒答） |
| `eval/suites/phase1.yaml` | Kubernetes-only Benchmark 套件 |

## 运行前准备

1. 真实单集群可达（`kubectl` 可用），并已部署只读 Connector 与 Agent Service。
2. Agent Service 启动时设置 `TRACE_DIR`，让 Eval Runner 能按 `diagnosis_id` 收集 Trace：
   ```powershell
   set TRACE_DIR=D:\AI\k8sPilot\eval-reports\trace
   ```
3. 保持 Agent Service 运行（默认 `http://localhost:8000`）。

## 冻结基线

```powershell
cd D:\AI\k8sPilot
python -m eval run --suite phase1 --runs 5 --profile baseline --trace-dir D:\AI\k8sPilot\eval-reports\trace
```

输出到 `reports/baseline-<ts>/`：
- `run.json` — 运行元数据（model、prompt_hash、tool_schema_hash、k8s 版本、suite、case 版本）
- `case-results.jsonl` — 逐次不可变结果（可离线复算报告）
- `report.json` + `report.md` — 汇总（按故障类型分组，含 Fixture/System/Incorrect 分层）

## 候选版本对照

对一次单变量改动（Prompt、模型、Tool Schema、裁剪规则）跑同套件：
```powershell
python -m eval run --suite phase1 --runs 5 --profile candidate --trace-dir ...\trace
python -m eval compare --baseline baseline-<ts> --candidate candidate-<ts> --reports-dir reports
```

对比报告覆盖：根因准确率、错误根因率、证据召回、耗时/Token P50/P95，并给出门槛判定（关键 Case 无回归、Wrong Root Cause Rate 不上升、结构化输出合法率不下降）。

## 复用 Phase 2 基准做 Phase 3 消融

Phase 3 引入 Prometheus/Loki 时，用同一 `--suite phase1` 跑 `K8s`、`K8s+Prometheus`、`K8s+Loki`、`K8s+Prometheus+Loki` 四组对照，证明每个数据源的净收益。

## 验收标准（设计 §23.10）

1. 一条命令完成 注入→判稳→诊断→Trace→评分→清理→报告，无人工改结果。
2. 每 Case ≥5 次；报告区分 Agent/系统/Fixture 错误，可从 `diagnosis_id` 定位 Tool 路径。
3. 同一份 `case-results.jsonl` 可离线重算出相同 `report.json`。
4. 候选改动生成 Baseline/Candidate 成对报告，列出改善/回归/成本变化。
5. Phase 1 三条 Headlamp 端到端场景（OOM/ImagePull/FailedScheduling）继续通过——Eval Report 不能替代产品链路验收。

## 已知边界

- Trace 为轻量 JSONL（设计 §32 允许），未部署 OTLP Collector；Phase 2 不以完整观测平台为验收前提。
- Scorer 纯确定性，不引入 LLM 盲审；同义根因覆盖问题留给独立盲审维度。
- `kubectl` 缺失或集群不可达时，Case 记为 `fixture_failed`（不计入准确率）。
