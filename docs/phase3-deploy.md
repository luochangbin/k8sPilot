# Phase 3 部署与验收说明

## 当前状态

| 组件 | 状态 |
|---|---|
| Connector 代码（capabilities / query_metrics / query_logs） | ✅ 已实现，22 单测通过 |
| Agent 代码（capability 门控 / Deployment·Node·PVC 泛化 / 降级） | ✅ 已实现，27 单测通过 |
| SQLite 持久化 + 历史列表 API | ✅ 已实现，4 单测通过 |
| Headlamp 插件多资源入口 | ✅ 已构建 |
| Prometheus（+cadvisor+node-exporter） | ✅ 已部署验证：7 target 全 UP，connector `query_metrics` 能取到真实 Pod 内存 |
| Loki | ✅ 已部署，能接收推送（早期验证 8746 字节落库） |
| fluent-bit → Loki 按 Pod 日志推送 | ⚠️ **开放问题**：能 tail 文件、能连 Loki，但记录未产出；promtail 方案（k8s SD 零 target）已移除 |
| Connector phase3 镜像 | ✅ 已部署，capabilities 已验证为 true |

## 在 docker/kind 可用的机器上执行

### 1. 重建并加载 Connector 镜像

```bash
cd connector
docker build -t ai-agent-connector:phase3 .
kind load docker-image ai-agent-connector:phase3 --name <当前集群名>   # kind-eval-cluster
```

### 2. 部署（Connector 带数据源 URL + Agent 带 SQLite）

```bash
kubectl apply -f connector/deploy/connector-all.yaml    # 只读 RBAC + Deployment(phase3) + Service
kubectl apply -f agent-service/deploy/                  # agent 集群部署（如用）
kubectl -n k8spilot rollout status deploy/ai-agent-connector
```

### 3. 验证 capabilities=true

```bash
# 取 Connector Pod IP
IP=$(kubectl -n k8spilot get pod -l app=ai-agent-connector -o jsonpath='{.items[0].status.podIP}')
curl http://$IP:8080/capabilities
# 期望 {"kubernetes.resources":true,"prometheus.metrics":true,"loki.logs":true}
curl "http://$IP:8080/tools/query_metrics" -H 'Content-Type: application/json' \
  -d '{"target":{"kind":"Pod","namespace":"observability","name":"loki-0"},"metric":"memory","range_minutes":30}'
# 期望 summary 含 max/avg/latest，degraded_reason 为空
```

### 4. Agent（本地）重启以读取 .env 与 SQLite

```powershell
cd D:\AI\k8sPilot\agent-service
# .env 已含 CONNECTOR_BASE_URL 与 LLM key；如需 SQLite 历史：
$env:DIAGNOSIS_DB = "D:\AI\k8sPilot\eval-trace\diagnoses.db"
.\start-agent.ps1
```

验证历史：跑一次诊断后 `curl http://localhost:8001/api/v1/diagnoses`，重启 agent 后再次 `curl` 应仍能查到。

## 验收项

1. **K8s-only 回归**：`python -m eval run --suite phase1 --runs 5 --profile baseline ...` 不应比 Phase 2 基线退化（Connector 能力开启但 Prom/Loki 查询失败会以 degraded 记录，不影响 K8s 事实诊断）。
2. **增强场景**：对 OOM Pod 诊断时 Agent 按需调用 query_metrics 取到内存证据；Loki 可用时引用日志证据。
3. **降级场景**：断开 LOKI_URL 或 PROMETHEUS_URL 后，诊断仍 completed，结果记录数据源不可用，不虚构证据。
4. **跨重启历史**：见上。
5. **多资源入口**：Headlamp 的 Deployment/Node/PVC 详情页出现「智能诊断」。

## 已知开放问题

- **按 Pod 推送日志到 Loki 未打通（问题 A，open）**：已精确定位——**不是采集端问题**。promtail/fluent-bit 均能正常 push（Loki 返回 HTTP 204，distributor 收到 16 万+ 行）；根因在 **Loki 端**：接受 204 但查询永远查不到（`ingester.totalReached=0`、store 0 chunk，`/ready` 最终 200 仍不可查）。手搓单实例试过 tsdb/v13 与 boltdb-shipper/v11 + `instance_addr` 两种配置现象一致；官方 Helm chart（6.55.0 / loki 3.6.7 singleBinary）因 kind 无 StorageClass + chart persistence=false 把 /var/loki 挂成只读而 CrashLoop。**建议的修复**：为 kind 加 local-path StorageClass 后改用 chart 默认 persistence 安装，或在有正规存储/网络的环境中跑 Loki 3.x 单实例。**不影响** Prometheus 指标增强与降级路径验收。
- 集群承载 VM 网络偶发不可达（API server EOF）与 registry 大镜像拉取不稳，操作需在集群可达时进行。

## 部署文件

- 拆分源：`observability/prometheus/prometheus.yaml`、`node-exporter.yaml`、`observability/loki/loki.yaml`、`fluent-bit.yaml`
- **合并清单**：`observability/observability-all.yaml`（16 资源，`kubectl apply -f` 一次到位）；`-mirror.yaml` 为 DaoCloud 国内镜像版
