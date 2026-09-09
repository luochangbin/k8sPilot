# Phase 1 端到端验收文档

> 验收要求：从 Headlamp 点击开始，到页面出现结构化结果结束；整条链路不使用 Mock。本机无 kubectl/docker/真实集群，需在真实集群上执行。

## 0. 前置条件

- 一个可写可读的 Kubernetes 集群（`kubectl` 可用）。
- 一个 OpenAI 兼容的 LLM 端点（支持 function calling），准备好 `base_url`、`api_key`、`model`。

## 1. 构建并部署

### 1.1 Connector（集群内，只读）

```bash
cd connector
docker build -t ai-agent-connector:phase1 .
kubectl apply -f deploy/                 # namespace + SA + 只读 RBAC + Deployment + Service
kubectl -n k8spilot rollout status deploy/ai-agent-connector
kubectl -n k8spilot exec deploy/ai-agent-connector -- wget -qO- http://localhost:8080/healthz
```

### 1.2 Agent Service（集群内）

```bash
cd agent-service
docker build -t agent-service:phase1 .
kubectl apply -f deploy/namespace.yaml 2>/dev/null || true
kubectl -n k8spilot create secret generic agent-service-secret \
  --from-literal=LLM_API_KEY=<your-key> --dry-run=client -o yaml | kubectl apply -f -
# 按需修改 deploy/configmap.yaml 中的 LLM_BASE_URL / LLM_MODEL
kubectl apply -f deploy/
kubectl -n k8spilot rollout status deploy/agent-service
```

### 1.3 Headlamp + 插件

- 本地运行 Headlamp，挂载插件 `dist/`：
  ```bash
  cd headlamp-plugin/ai-diagnosis-plugin && npm run build
  headlamp server --plugins-dir <abs>/ai-diagnosis-plugin/dist
  ```
- 或用桌面版 Headlamp，将插件目录加入插件路径。
- 确保浏览器可访问 `http://localhost:8000`（Agent Service）；如否，按插件 README 设置 `window.__K8S_PILOT_AGENT_BASE__`。

## 2. 故障注入

### 2.1 OOMKilled（容器内存上限不足）

```bash
kubectl create ns diag
kubectl -n diag apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: oom-demo
  namespace: diag
  labels: { app: oom-demo }
spec:
  containers:
    - name: app
      image: busybox
      command: ["sh", "-c", "i=0; while true; do i=$((i+1)); done"]   # 疯狂占用内存
      resources:
        limits: { memory: 32Mi }
EOF
# 预期：容器进入 CrashLoopBackOff，LastState=OOMKilled
kubectl -n diag get pod oom-demo
```

### 2.2 ImagePullBackOff（镜像拉取失败）

```bash
kubectl -n diag apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: imagepull-demo
  namespace: diag
  labels: { app: imagepull-demo }
spec:
  containers:
    - name: app
      image: registry.example.com/nonexistent:v9
EOF
# 预期：Events 出现 ErrImagePull / ImagePullBackOff
```

### 2.3 FailedScheduling（无法调度）

```bash
kubectl -n diag apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: schedule-demo
  namespace: diag
spec:
  containers:
    - name: app
      image: busybox
      command: ["sh", "-c", "sleep 3600"]
  nodeSelector:
    gpu: "true"          # 集群中不存在的标签 → 永远 Pending
EOF
# 预期：Pod 处于 Pending，Events 出现 FailedScheduling
```

## 3. 验收步骤（每个故障各一次）

1. Headlamp → Workloads → Pods → 打开故障 Pod 详情页。
2. 点击「智能诊断」。
3. 观察调查进度滚动（获取 Pod 状态 → 查询事件 → 查询日志 → 分析关联资源）。
4. 等待出现结构化结果。

### 通过标准

| 故障 | 期望结果 |
|---|---|
| OOMKilled | `root_cause` 指向内存超限（如容器内存 limit 过低 / OOMKilled）；证据含 last termination reason=OOMKilled |
| ImagePullBackOff | `root_cause` 指向镜像拉取失败（镜像不存在/仓库认证等）；证据含 Events ErrImagePull/ImagePullBackOff 与 ContainerState |
| FailedScheduling | `root_cause` 指向调度失败（如节点标签不匹配），或**证据不足声明**（completed + 空 root_cause + missing_evidence），两者皆可 |

所有故障都必须展示：symptom、evidence、confidence、recommendations。失败（failed）不应出现在上述三类注入场景。

## 4. 失败边界抽查

- 删除 Pod 后重建（UID 变化）再点「智能诊断」→ 诊断拒绝并提示资源已重建。
- 临时停掉 Connector Deployment → 新诊断应 `failed` 且错误信息可定位。
- 停止 Agent Service → Headlamp 页面应显示可读错误。

## 5. 非验收目标（Phase 1）

Prometheus/Loki 未安装时系统必须正常启动并完成上述闭环；多集群、告警触发、修复操作不在本 Phase 验收范围。
