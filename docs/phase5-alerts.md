# Phase 5：告警自动诊断

状态：**已实现**（告警 Webhook → 去重/生命周期 → 自动诊断链路已真机验证；Alertmanager 与 Connector 镜像的集群部署待环境接入）。

目标：在人工诊断继续可用的基础上接入 Alertmanager，实现告警目标解析、自动诊断、`fingerprint` 去重、恢复与重新触发；**不生成修复计划、不执行写操作**。

## 架构

```text
Alertmanager ──(webhook)──▶ Connector POST /alerts
                               │ 目标解析 + 轻量快照（Inspect + 限量 Events）
                               ▼
                        Agent Service POST /api/v1/diagnoses (trigger=alert)
                               │ fingerprint 去重 / 生命周期 / 告警风暴限流
                               ▼
                        Diagnosis Session（复用现有 Agent Loop / Connector Tool / Result）
```

- 告警接收在 **Connector**（设计 §14/§15/§18）；去重与生命周期在 **Agent Session Store**（设计 §16/§26.2）。
- 复用 `DiagnosisRequest`：新增 `trigger=alert` 与 `alert` 上下文，不建立第二套诊断流程。
- 只做轻量快照（资源 metadata/status/conditions/anomalies + 限量 Events），不做全量 Metrics/Logs 抓取。

## Agent 侧（`agent-service`）

- `POST /api/v1/diagnoses`，`trigger=alert`，请求体含 `alert`：
  - `fingerprint`、`status`（firing/resolved）、`alertname`、`starts_at`、`group_key`、`labels`、`annotations`、`snapshot`、`unresolved_target`；
  - 目标告警同时带 `resource`（由 Connector 解析并补 `uid`）。
- 生命周期规则（§26.2）：
  - 同一 `fingerprint` 在 firing 生命周期内重复到达 → 仅更新 `latest_alert_at`，返回既有 `diagnosis_id`（响应 `deduped=true`），**不创建并行诊断**；
  - `resolved` → 关闭当前生命周期；
  - 关闭后再次 firing → 创建新的生命周期与诊断；
  - 无法唯一映射资源 → 记 `unresolved_target`，**不猜测目标、不建诊断**。
- 告警风暴限流：`ALERT_RATE_LIMIT_PER_MINUTE`（默认 30，0 关闭），超限返回 429；不影响人工诊断路径。
- 存储：`alert_lifecycles` 表（`fingerprint, diagnosis_id, state(open|closed|unresolved_target), alertname, target, labels, starts_at, latest_alert_at, resolved_at`）。
- 响应：`{"accepted","unresolved","failed","results":[{fingerprint,status,diagnosis_id,deduped}]}`。

## Connector 侧（`connector`）

- `POST /alerts` 接收 Alertmanager webhook v4；逐条处理，单条失败不影响其余。
- `ResolveTarget` 仅接受可诊断类型（Pod/Deployment/Node/PVC）：显式 `kind` 标签或 `pod/deployment/node/persistentvolumeclaim` 标签推断；缺 namespace / 不支持类型 → unresolved。
- 轻量快照后转发 Agent；`fingerprint` 缺失时按 labels 派生稳定值。
- 配置：`AGENT_URL`（默认 `http://localhost:8000`）、`ALERT_FORWARD_TIMEOUT_SECONDS`（默认 10）、`ALERT_SNAPSHOT_EVENT_LIMIT`（默认 10）、`ALERT_WEBHOOK_TOKEN`（共享 Bearer Token，空则关闭鉴权）、`ALERT_MAX_BATCH`（单批告警上限，默认 100）。
- 安全：`POST /alerts` 可触发付费诊断，因此
  - 配置 `ALERT_WEBHOOK_TOKEN` 后要求 `Authorization: Bearer <token>`（常量时间比较），Token 经 Secret 注入（`deploy/*.yaml` 中 `secretKeyRef`，可选键）；
  - 单批告警数超过 `ALERT_MAX_BATCH` 返回 413；
  - 提供 `deploy/networkpolicy-alerts.yaml` 模板限制入站（注意：同端口也服务 Agent 的 Tool 调用，策略必须同时放行 Alertmanager 与 Agent 来源，否则会中断诊断）。
- 转发失败语义：任一告警转发失败时 **不返回 202**——纯 Agent 429 透传 429，其余失败返回 502（`Summary.RetryableStatus()`），让 Alertmanager 继续重试，避免丢失 resolved 导致生命周期长期 open。

## 审阅修正（2026-09-16）

针对外部审阅的 7 个问题（4×P1、3×P2）已核实并修复（均属实）：

1. **并发 firing 重复诊断（P1）**：`SessionStore.claim_active_alert_lifecycle` 在单锁内原子完成「查活跃生命周期→更新或插入」，新增**部分唯一索引** `uniq_alert_lifecycle_active(fingerprint) WHERE state IN ('open','unresolved_target')`；先占用生命周期，再创建诊断并回填 `diagnosis_id`。并发同 fingerprint 只产生一个诊断。
2. **转发失败仍返回 202（P1）**：`Summary.RetryableStatus()`——无失败 202、纯 429 透传 429、其余 502；Alertmanager 可重试，不再丢 resolved。
3. **resolved 被限流/误判 unresolved（P1）**：resolved 在**限流与目标解析之前**优先处理，只需 fingerprint、不消耗配额；无 `resource` 也能关闭生命周期。
4. **`/alerts` 无认证（P1）**：新增 `ALERT_WEBHOOK_TOKEN`（Bearer，常量时间比较，Secret 注入）、`ALERT_MAX_BATCH` 批量上限（413）、NetworkPolicy 模板。
5. **迟到 resolved 关闭新生命周期（P2）**：关闭时校验 `starts_at`，与活跃生命周期不一致则忽略（返回 `resolved_noop`）。
6. **重复 unresolved 不去重（P2）**：unresolved 也走生命周期占用（state=`unresolved_target`），重复到达只更新 `latest_alert_at`，resolved 后关闭，不再无限增长。
7. **touch/close 批量更新（P2）**：改为按生命周期 `id` 更新，配合唯一索引消除「多 open 行」的读写不一致。

测试补充（P3）：并发重复 firing 只建 1 条诊断、resolved 不受限流且无需 resource、迟到 resolved 不关闭新周期、重复 unresolved 去重、SQLite 重开持久化；Connector 侧新增 agent 失败→502、429 透传、缺 Token→401、超批量→413、`HTTPStatus` 记录与 `RetryableStatus` 映射。

### 生命周期修正（2026-09-16，第 2 轮）

再核实 2 个生命周期问题（均属实）并修复：

1. **诊断创建失败后无法重试（P1）**：占用生命周期后若创建诊断抛错，原实现会留下“空占用”，重试返回 `deduped=true, diagnosis_id=null` 且永不启动。现：`_start` 失败时 `release_alert_lifecycle`（仅删除**未关联诊断**的占用）后重新抛出，重试可正常启动；并发安全仍由唯一索引 + 单锁保证；对崩溃遗留的空占用，超过 `ALERT_CLAIM_STALE_SECONDS`（默认 300）允许**接管**。
2. **unresolved 生命周期挡住可解析目标（P2）**：原实现把 `unresolved_target` 与已启动诊断一并去重。现新增 `upgrade_unresolved_lifecycle`：同一 fingerprint 在目标变得可解析时，把未关联诊断的 unresolved 生命周期**提升为 open** 并启动一次诊断；已升级后再次 firing 正常去重。resolved 仍可关闭 open/unresolved 两类活跃生命周期。

测试补充：诊断创建失败后可重试（占用被释放且重试成功）、unresolved→可解析目标升级并只建 1 条诊断、陈旧空占用可被接管。`agent-service` 全量 **66 passed**。

### 接管原子性修正（2026-09-16，第 3 轮）

再核实 1 个并发问题（属实）并修复：

- **过期占位接管不具原子性（P1）**：原实现判断过期后直接启动，多个并发同 fingerprint 请求会各自启动诊断。现改为**原子条件接管**：`takeover_stale_alert_lifecycle` 用条件 `UPDATE`（要求 `state='open' AND diagnosis_id IS NULL AND created_at <= cutoff`）并在**成功时刷新 `created_at`（占位期限）**；只有 `rowcount==1` 的请求取得接管权并启动诊断，其余返回 `in_progress`/去重。刷新 `created_at` 而非仅 `latest_alert_at` 是关键：持续 firing 只刷新 `latest_alert_at`，因此“崩溃后一直有 firing”的场景仍能触发接管，且并发时只有一人获胜。

测试补充：**并发（5 路）接管陈旧占位只启动 1 条诊断**，其余返回 `deduped`。`agent-service` 全量 **67 passed**；`eval` 39 passed。

### unresolved 升级原子性修正（2026-09-16，第 4 轮）

再核实 1 个遗漏分支（属实）并修复：

- **旧 unresolved 升级后仍可能重复启动（P1）**：`upgrade_unresolved_lifecycle` 原先只更新 `updated_at`，未刷新过期判定所用的 `created_at`；若该 unresolved 生命周期较旧，其他并发请求在其升级后会判定为“过期占位”并再次接管，导致同一 fingerprint 出现两条诊断。现：升级时**同步刷新 `created_at`（占位期限）**，并在升级失败（他人已抢先）后**重新读取该行**再进入去重/过期判定，避免基于旧快照决策。

测试补充：**旧 unresolved（已回拨 `created_at`）在 5 路并发下升级只启动 1 条诊断**。`agent-service` 全量 **68 passed**；`eval` 39 passed。

## 验证记录

自动化测试：

- `agent-service`：`tests/test_alerts.py` **10 项**（重复 firing 合并、resolved+refire、unresolved、并发单诊断、resolved 不受限流/无需 resource、迟到 resolved、重复 unresolved 去重、持久化、风暴限流 429、缺 context 400）→ 全量 **63 passed**。
- `connector`：`internal/alerts/alerts_test.go` 5 项 + `internal/server/alerts_test.go` 5 项（失败→502、429 透传、缺 Token→401、超批量→413、`HTTPStatus`/`RetryableStatus`）→ `go build ./...` / `go test ./...` / `go vet ./...` 全过。

真机闭环（本地 Connector 8081 → Agent 8000 → 集群，`aiops-eval/eval-crashloop`，模拟 Alertmanager 推送）：

| 步骤 | 结果 |
|---|---|
| 1. firing `fingerprint=demo-fp-1` | 新建诊断 `diag_e747f68a390f`，`completed`，`CONFIG_ERROR`（实时证据支撑） |
| 2. 重复 firing（同 fingerprint） | `deduped=true`，返回同一 `diagnosis_id`，未新建并行诊断 |
| 3. resolved | `status=closed`，生命周期关闭 |
| 4. 再次 firing | 新诊断 `diag_23f9c94fd4f8`（新生命周期） |

落库核对：`alert_lifecycles` 两行（id=1 `closed`/有 diagnosis、id=2 `open`/新 diagnosis）；`diagnoses` 两条 `trigger=alert`。

## 未验证 / 限制

- 未接真实 Alertmanager（用等价 webhook 载荷模拟）；未在集群内运行更新后的 Connector 镜像（无本地 docker），部署路径仅更新了配置/Secret/NetworkPolicy 模板。
- `ALERT_WEBHOOK_TOKEN` 鉴权与 `ALERT_MAX_BATCH` 仅在单测覆盖，未在集群联调。
- `deploy/networkpolicy-alerts.yaml` 为模板，未应用（且必须同时放行 Agent 来源，否则会中断 Tool 调用）。
- 告警风暴限流的真机压测未做（仅单测覆盖）。
- 未覆盖 Node/PVC/Deployment 告警目标的真机闭环（目标解析有单测）。
- `go test -race` 本机不可运行（需要 CGO/gcc）；建议在支持 CGO 的 CI 中执行 `go test -race ./...`。
