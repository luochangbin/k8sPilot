# Kubernetes AIOps 智能诊断平台设计总结

## 1. 项目定位

目标是在现有 Kubernetes 管理平台中集成 AIOps 智能诊断能力，而不是单独做一个 ChatBot。

核心用户体验（按阶段逐步交付，最终形态）：

- Phase 1 已完成单集群 Pod 详情页「智能诊断」闭环，作为后续能力演进的固定基线。
- Phase 2 建立评测、基线、回归和能力改进闭环，不扩展新的生产诊断数据源或资源入口。
- Phase 3 扩展到 Deployment、Node、PVC，并按需引入 Prometheus、Loki 与持久化历史。
- Phase 4 增加专业知识 RAG 与历史 Incident 检索，为假设生成和调查路径提供带引用的辅助上下文。
- 用户点击后，Agent 自动基于当前资源上下文进行诊断。
- Prometheus 告警触发后，系统自动启动诊断，无需人工点击（Phase 5 引入，Phase 1 不包含）。
- Agent 可以联合分析（按可用能力按需使用）：
  - Kubernetes API（资源状态、Conditions、Events、Pod Logs）—— Phase 1
  - Prometheus Metrics —— Phase 3 可选能力
  - Loki Logs —— Phase 3 可选能力
- 最终输出：
  - 当前症状
  - 根因
  - 关键证据
  - 影响范围
  - 诊断置信度
  - 修复建议
  - 参考知识与相似历史 Incident（Phase 4 起，独立于实时 Evidence）
- 后续 Phase 逐步增加（Phase 1 不包含）：
  - Phase 2：评测、基线、回归与能力改进
  - Phase 3：单集群丰富诊断与历史
  - Phase 4：专业知识 RAG 与历史 Incident 检索
  - Phase 5：告警自动诊断
  - Phase 6：修复计划生成、人工确认
  - Phase 7：受控自动修复

演进原则：每个 Phase 都是可独立部署、启动、演示和验收的产品增量，只依赖当前 Phase 或此前 Phase 已交付的能力；任何阶段不得依赖未来阶段才能启动或完成自身验收（详见「21. 阶段演进模型」）。

整个产品的核心不是：

> 给 LLM 一个 kubeconfig，让它执行 kubectl。

而是：

> 将 Kubernetes 状态、指标、日志等能力结构化提供给 Agent，由 Agent 自主进行多步调查和根因分析。


---

# 2. Kubernetes 管理平台选择

不建议从零开发 Kubernetes 管理平台。

需要开发的核心能力是 AIOps，而不是：

- 登录
- Namespace 页面
- Pod 页面
- Deployment 页面
- Node 页面
- YAML 编辑器
- Terminal
- Logs
- Events
- RBAC
- Resource Table

这些能力已经有成熟的开源项目。

## 2.1 Headlamp

当前最适合作为 PoC / MVP 底座。

Headlamp 是 Kubernetes SIG 下的 Kubernetes Web UI，支持：

- Kubernetes 资源管理
- Pod Logs
- Exec
- YAML
- Resource Detail
- Plugins
- Prometheus 插件
- AI Assistant 插件
- Resource Map

最大的优势是插件机制。

因此可以不修改 Headlamp 核心代码，而是开发：

    ai-diagnosis-plugin

在资源页面增加：

    [日志] [事件] [YAML] [智能诊断]

推荐当前项目优先基于 Headlamp 开发。


## 2.2 KubeSphere

适合未来向企业级 Kubernetes 管理平台演进。

优点：

- Workspace
- RBAC
- Monitoring
- DevOps
- Extension Marketplace
- 前后端扩展机制

适合未来产品化，但 PoC 阶段相对较重。


## 2.3 Rancher

企业级 Kubernetes 管理平台。

优点：

- Kubernetes 生命周期管理成熟
- 企业权限
- Extension 机制

但系统复杂度远高于当前 PoC 所需，不适合作为第一版开发底座。


## 2.4 当前建议

优先级：

| 平台 | 推荐程度 | 使用场景 |
|---|---|---|
| Headlamp | ★★★★★ | PoC / MVP |
| KubeSphere | ★★★★☆ | 企业产品 |
| Rancher | ★★★☆☆ | 大型企业 Kubernetes 管理 |
| Kubernetes Dashboard | 不推荐 | 已归档 |

当前推荐：

    Headlamp + AI Diagnosis Plugin


---

# 3. 整体架构

按阶段累积演进：已完成的 Phase 1 采用最小拓扑，仅连接 Kubernetes API；Phase 2 在同一单集群链路外增加评测与基础可观测能力；Prometheus 与 Loki 作为 Phase 3 起的可选 Connector capability 加入；Phase 4 再引入独立 Knowledge & Experience Service。后续能力不得成为此前 Phase 的启动条件。

Phase 1 拓扑：

    Kubernetes 管理平台
            |
            |
         Agent
            |
            |
    ai-agent-connector
            |
            |
         K8s API

Phase 3 起，Connector 按 capabilities 可选连接扩展数据源：

    ai-agent-connector
        /      |      \
       /       |       \
    K8s API Prometheus Loki


按独立部署组件划分：

## 集群外

### Headlamp / Kubernetes 管理平台

负责：

- Kubernetes UI
- Resource 页面
- 「智能诊断」入口
- Diagnosis Result 展示
- 告警中心
- Diagnosis Session 展示


### Agent Service

负责：

- LLM
- Tool Calling
- Diagnosis Loop
- 故障假设
- 调查规划
- 根因分析
- 证据归纳
- 修复建议


### Knowledge & Experience Service（Phase 4）

负责：

- Kubernetes/产品文档、Runbook、SOP、已知问题和架构文档的版本化摄取与检索
- 已验证历史 Incident 的相似 Case 检索
- 来源、版本、时间、权限和引用信息返回
- 检索质量与知识新鲜度指标

该服务不访问 Kubernetes API，不执行诊断或修复，也不能把知识检索结果伪装成实时 Evidence。


## 目标 Kubernetes 集群内

### ai-agent-connector

以 Deployment 方式部署。

负责：

- Kubernetes API 查询
- Prometheus 查询
- Loki 查询
- Alertmanager Webhook 接收
- 基础数据采集
- 数据裁剪
- 数据结构化
- 对 Agent 暴露统一 API

Connector：

    不调用 LLM
    不需要 LLM API Key

Connector 使用 ServiceAccount 访问 Kubernetes。


---

# 4. 人工智能诊断入口

Phase 1 仅 Pod 详情页提供「智能诊断」入口；Phase 2 保持该用户边界并围绕现有能力建立评测闭环；Phase 3 起扩展到 Deployment、Node、PVC 等资源。

最终形态下，每种 Kubernetes Resource 页面都显示：

    [智能诊断]

例如：

    Pod: payment-api-7b8c9

    Status: CrashLoopBackOff
    Restart: 37
    Node: node-17

    [Logs]
    [Events]
    [YAML]
    [智能诊断]

点击以后，管理平台不应该只发送：

    帮我诊断 payment-api

而应该直接将 Resource Identity 发送给 Agent：

    {
      "trigger": "manual",

      "resource": {
        "apiVersion": "v1",
        "kind": "Pod",
        "namespace": "payment",
        "name": "payment-api-7b8c9",
        "uid": "..."
      }
    }

这样 Agent 不需要再判断：

- payment-api 是 Pod 还是 Deployment
- 属于哪个 Namespace

管理平台天然知道这些信息。


---

# 5. 统一 Diagnosis Request

人工触发和告警触发最终统一成：

    Diagnosis Request

`DiagnosisRequest` 包含完整资源身份（含 `uid`）。本项目边界固定为单集群，不在公共契约中加入集群注册、跨集群路由或隔离字段。

## 人工触发

    {
      "trigger": "manual",

      "resource": {
        "apiVersion": "v1",
        "kind": "Pod",
        "namespace": "payment",
        "name": "payment-api-7b8c9",
        "uid": "550e8400-e29b-41d4-a716-446655440000"
      }
    }


## Alert 触发（Phase 5 引入）

    {
      "trigger": "alert",

      "resource": {
        "kind": "Pod",
        "namespace": "payment",
        "name": "payment-api"
      },

      "alert": {
        "name": "PodHighMemory"
      }
    }

告警触发复用人工诊断的创建入口，不建立第二套诊断流程；Phase 1 仅支持人工触发。

架构：

                      Diagnosis Request
                              |
                  +-----------+-----------+
                  |                       |
             Manual Trigger          Alert Trigger
                  |                       |
                  +-----------+-----------+
                              |
                              v
                         Agent Service
                              |
                       Diagnosis Loop
                              |
                              v
                          Connector
                              |
                 +------------+------------+
                 |            |            |
                 v            v            v
              K8s API     Prometheus      Loki

（说明：Alert Trigger 属 Phase 5，Prometheus/Loki 属 Phase 3 可选能力，知识与历史检索属 Phase 4；Phase 1 仅 Manual Trigger + K8s API；Phase 2 评测该既有链路。）


---

# 6. 不采用「每种资源一个 Analyzer」

最初可以参考 K8sGPT Analyzer 的思想，但不建议最终采用：

    PodAnalyzer
    DeploymentAnalyzer
    NodeAnalyzer
    PVCAnalyzer
    ServiceAnalyzer
    StatefulSetAnalyzer
    ...

原因：

随着 Kubernetes Resource 和 CRD 增长，会形成大量：

- Analyzer
- Rule
- Check
- Tool

维护成本越来越高。

最终容易变成：

    Expert System + LLM Explanation

而不是：

    Intelligent Diagnosis Agent


---

# 7. 通用诊断能力

Connector 应该尽量只提供通用的实时数据能力。

Agent 只有少量通用 Tool：

    inspect
    relations
    events
    logs        （Phase 1 通过 Kubernetes Pod Logs API 获取）
    metrics     （Phase 3 可选 capability：prometheus.metrics）
    loki.logs   （Phase 3 可选 capability）

Phase 1 的 Connector 提供 inspect、relations、events、logs；Phase 2 只评测和改进这些既有能力；metrics 与 Loki 日志在 Phase 3 作为可选 capability 加入，Agent 先查询 capabilities，再决定是否调用。

Phase 4 由独立 Knowledge & Experience Service 增加：

    search_knowledge
    search_incidents

两类检索 Tool 不属于 Connector capability，也不能返回或冒充当前集群事实。


## 7.1 inspect

查询资源当前状态。

例如：

    inspect(
        kind="Pod",
        namespace="payment",
        name="payment-api"
    )

返回：

    metadata
    spec
    status
    conditions
    ownerReferences

可以统一归一化：

    {
      "target": "...",

      "desired_state": {},

      "actual_state": {},

      "conditions": [],

      "anomalies": []
    }


---

# 8. Desired State vs Actual State

Kubernetes 是声明式系统，因此很多问题可以统一理解为：

    Desired State
          |
          | diff
          v
    Actual State

例如：

## Deployment

    Desired:
      replicas = 5

    Actual:
      readyReplicas = 2


## PVC

    Desired:
      Bound

    Actual:
      Pending


## Node

    Desired:
      Ready

    Actual:
      Ready=False


## Job

    Desired:
      Complete

    Actual:
      Failed

因此可以设计通用：

    State Normalizer

而不是大量 Resource Analyzer。


---

# 9. Resource Relations

Kubernetes 不应该只被看成很多 Resource，而应该看成 Resource Graph。

例如：

    Deployment
        |
       owns
        |
        v
    ReplicaSet
        |
       owns
        |
        v
       Pod
      / |  \
     /  |   \
    v   v    v

  Node PVC Service


Agent 可以使用：

    relations(target)

查询资源关系。

例如 Pod 可以关联：

- Deployment
- ReplicaSet
- Node
- Service
- PVC
- ConfigMap
- Secret
- ServiceAccount

这允许 Agent 沿资源关系进行调查。


---

# 10. Events

所有 Kubernetes Resource 都可以统一使用：

    events(target)

例如：

    FailedScheduling

    FailedMount

    BackOff

    FailedCreate

    Unhealthy

Events 本身已经包含大量高价值的故障语义。

因此 Agent 应优先利用：

    Resource Status
    +
    Conditions
    +
    Events

然后再查询 Pod Logs（Phase 1）或 Metrics / Loki Logs（Phase 3 可选能力）。

Phase 1 的诊断事实仅来自 Kubernetes API，包括资源状态、Conditions、Events、Pod Logs 和必要关联资源。


---

# 11. Prometheus

Prometheus 负责指标。本能力属于 Phase 3 可选 Connector capability `prometheus.metrics`；Phase 1 与 Phase 2 均不依赖 Prometheus。数据源不可用、超时或返回截断时必须记录在结果中，并继续使用已有证据。

例如：

- CPU
- Memory
- Disk
- Network
- Pod Restart
- kube-apiserver latency
- etcd latency
- Request QPS
- APF
- Node metrics
- Application metrics

Agent 可以调用：

    query_metrics(
        target=...,
        question=...
    )

例如：

    查询 payment-api 最近30分钟内存情况


Connector 内部查询 Prometheus。

第一版可以支持两种方式：

### 固定指标 Tool

例如：

    get_pod_cpu
    get_pod_memory
    get_apiserver_latency

优点：

- 稳定
- 安全
- 可控


### 后续支持动态 PromQL

Agent 生成 PromQL：

    Agent
      |
      v
    Connector
      |
      v
    Prometheus

Connector 负责：

- 校验
- 超时
- 查询时间范围
- 最大返回数据量


---

# 12. Loki

Loki 提供日志能力。Phase 1 与 Phase 2 不依赖 Loki，日志通过 Kubernetes Pod Logs API 获取；Loki 日志证据属于 Phase 3 可选 capability `loki.logs`。Loki 不可用时回退到 Phase 1 的 Kubernetes 事实诊断，并在结果中明确标识。

Agent 可以调用：

    query_logs(
        target=Pod,
        range=30m
    )

Connector 不应该直接把数万行日志交给 LLM。

需要进行：

- 日志数量限制
- 时间范围限制
- Error / Warning 优先
- 去重
- Pattern 聚合
- 截断

返回：

    {
      "summary": {
        "total_lines": 18000,
        "error_lines": 560,

        "top_patterns": [
          "connection refused",
          "OutOfMemoryError"
        ]
      },

      "evidence": [
        "...",
        "..."
      ],

      "truncated": true
    }

核心原则：

    Agent 需要信息密度
    而不是原始数据量


---

# 13. Agent Diagnosis Loop

Agent 不应该一次性查询所有数据。

推荐使用自主调查循环：

    Target Resource
          |
          v
    inspect resource
          |
          v
    inspect relations
          |
          v
      get events
          |
          v
    建立故障假设
          |
     +----+----+
     |         |
     v         v
  metrics     logs
     |         |
     +----+----+
          |
          v
       验证假设
          |
     +----+----+
     |         |
    否        是
     |         |
 新假设     Root Cause
     |
     +-----> Loop


例如：

用户：

    payment-api 为什么一直重启？

Agent：

    inspect(payment-api)

发现：

    Deployment desired=5
    ready=2

继续：

    relations(payment-api)

发现：

    Pod-A RestartCount=53

继续：

    inspect(Pod-A)

发现：

    LastState=OOMKilled

继续：

    metrics(Pod-A)

发现：

    Memory=1.95Gi
    Limit=2Gi

继续：

    logs(Pod-A)

发现：

    java.lang.OutOfMemoryError

    最终：

    Root Cause:
    Container Memory Limit 过低

说明：示例中 metrics 与日志证据为 Phase 3 可选能力；Phase 1 的调查循环仅使用 Kubernetes 事实（inspect、relations、events、logs），Phase 2 对该循环进行评测和改进，日志来自 Kubernetes Pod Logs API，不依赖 Prometheus/Loki。

Phase 4 不在调查开始时无条件检索全部知识。Agent 应先通过实时 Tool 建立最小事实集和候选假设，只在存在产品知识缺口或需要相似经验时调用 search_knowledge/search_incidents，再通过新的实时 Tool Evidence 验证检索启发的假设。


---

# 14. Prometheus Alert 自动诊断

告警自动诊断在 Phase 5 引入；Phase 1～4 均不接收告警。

Prometheus 告警链路建议：

    Prometheus
        |
        v
    Alertmanager
        |
        +----> 原有告警渠道
        |
        |      Email
        |      SMS
        |      Slack
        |      DingTalk
        |
        +----> ai-agent-connector
                    |
                    v
             Diagnosis Event
                    |
                    v
               Agent Service


不建议：

    Prometheus
      |
      +--> Alertmanager
      |
      +--> Connector

推荐让 Alertmanager 统一 fan-out。

原因是 Alertmanager 已经负责：

- Dedup
- Grouping
- Routing
- Silence
- Inhibition


---

# 15. Connector 收到 Alert 后做什么

本节描述 Phase 5 告警自动诊断行为；Phase 1～4 的 Connector 不接收告警。

Connector 收到告警后：

不要立即采：

- 所有 Metrics
- 所有 Logs
- 所有关联资源
- 全量 Events

否则 Connector 会越来越像复杂 Analyzer。

推荐：

    Alert
      |
      v
    Connector
      |
      v
    Lightweight Snapshot
      |
      v
    Agent
      |
      v
    按需调查


基础快照可以包括：

    Alert Labels

    Alert Annotations

    Resource metadata

    Resource status

    Conditions

    ownerReferences

    Recent Events


例如：

    {
      "alert": {
        "name": "PodHighMemory"
      },

      "target": {
        "kind": "Pod",
        "namespace": "payment",
        "name": "payment-api"
      },

      "snapshot": {
        "phase": "Running",
        "restartCount": 12,
        "node": "node17"
      }
    }

然后 Agent 再决定：

    是否查 Metrics

    是否查 Logs

    是否查 Node

    是否查 Deployment

    是否查 Service

这样能够降低：

- API Server 压力
- Prometheus 压力
- Loki 压力
- Token 消耗


---

# 16. 告警去重

本节为 Phase 5 告警自动诊断的去重与生命周期设计。

告警自动诊断必须考虑：

    Alert Storm

例如同一个告警不断 firing：

    20:00
    20:01
    20:02
    20:03

不能创建四个 Diagnosis。

应该利用：

    Alertmanager fingerprint

或者：

    groupKey

建立：

    Diagnosis Session


例如：

    fingerprint = abc123

    Diagnosis #123

    state = investigating

后续同一 fingerprint：

    不创建新 Diagnosis

只更新：

    latest_alert_at


生命周期：

    firing
       |
       v
    Diagnosis Session
       |
       v
    Investigating
       |
       v
    Diagnosis Result
       |
       v
    resolved
       |
       v
    Closed

去重键为 `fingerprint`：同一告警生命周期内重复 firing 只更新最新时间（latest_alert_at），不创建并行诊断；resolved 关闭当前生命周期；之后重新 firing 创建新生命周期。无法唯一映射资源的告警进入 `unresolved_target`，不得猜测目标。


---

# 17. Diagnosis Session

Phase 1 使用进程内 Session 存储，服务重启后历史丢失是已知限制，不作为持久化能力宣传；Phase 2 的评测产物独立落盘，但不改变产品 Session 存储；Phase 3 引入持久化 Session，并提供重启后历史查询，且不迁移 Phase 1 的临时历史。

管理平台 UI 不应该只展示一段 LLM 文本。

应该有完整 Diagnosis Session。

例如：

    Pod: payment-api

    状态：
    CrashLoopBackOff


    AI Diagnosis

    当前症状
    ----------------
    Pod 持续重启


    调查过程
    ----------------
    ✓ 获取 Pod 状态

    ✓ 查询 Event

    ✓ 查询 Memory Metrics

    ✓ 查询日志

    ✓ 分析关联资源


    Root Cause
    ----------------
    Container Memory Limit 过低


    Evidence
    ----------------
    RestartCount = 37

    LastState = OOMKilled

    Memory Peak = 1.98Gi

    Memory Limit = 2Gi

    java.lang.OutOfMemoryError


    Confidence
    ----------------
    High


    Recommendation
    ----------------
    检查 JVM Heap 配置

    或提高 Container Memory Limit


    [生成修复计划]


这种设计比单纯 ChatBot 更适合 Kubernetes 管理平台。


---

# 18. Agent 与 Connector 的职责边界

## Agent

负责：

    Reasoning

    Planning

    Diagnosis Loop

    Hypothesis

    Evidence Correlation

    Root Cause Analysis

    Recommendation


## Connector

负责：

    K8s API Access
    Prometheus Access      （Phase 3 可选）
    Loki Access            （Phase 3 可选）
    Alert Webhook          （Phase 5）

    Data Normalization

    Data Limiting

    Query Timeout

    RBAC Boundary


核心原则：

    Connector 负责“获取事实”

    Agent 负责“理解事实”


---

# 19. 为什么 Agent 不直接使用 kubeconfig

不要：

    Agent
      |
    kubeconfig
      |
    cluster-admin
      |
    Kubernetes


推荐：

    Agent
      |
      v
    Connector
      |
      v
    ServiceAccount
      |
      v
    Kubernetes API


原因：

- Agent 不持有集群长期凭证
- 避免 cluster-admin 暴露
- 降低 Prompt Injection 风险
- 权限集中到 Connector
- 可以通过 RBAC 限制
- 方便审计


---

# 20. Connector RBAC

Phase 1 只提供 Read Only 权限。Phase 1～6 组件不携带 Kubernetes 写权限；Phase 7 才引入独立 Executor 部署边界执行写操作。

例如：

    get
    list
    watch


Resource：

    pods
    events
    nodes
    namespaces
    services
    endpoints
    persistentvolumeclaims

    deployments
    replicasets
    statefulsets
    daemonsets


第一版不允许：

    delete

    create

    patch

    update


---

# 21. 阶段演进模型

## 21.1 发布门槛

Phase 是一个可交付的产品版本，不是组件开发批次。每个 Phase 必须满足以下发布门槛：

1. 提供从空环境部署和启动该版本所需的全部产物与说明。
2. 至少有一个用户可见、端到端闭环的功能。
3. 只依赖当前 Phase 或此前 Phase 已发布的组件和接口。
4. 在真实 Kubernetes 环境完成验收；Mock 只能用于单元或组件测试。
5. 新增能力失败时有明确错误或降级行为，不能悄悄跳过。
6. 后续 Phase 发布时，之前 Phase 的验收场景必须继续通过。

## 21.2 累积演进顺序

| Phase | 独立可用能力 | 新增范围 | 明确不依赖 |
|---|---|---|---|
| 1（已完成） | 单集群 Pod 人工诊断 | Headlamp 入口、Agent、Connector、Kubernetes 事实、结构化结果 | Prometheus、Loki、告警、安全加固、自动修复 |
| 2（已完成） | Agent 评测与能力改进 | 基础 Trace、Eval Harness、Phase 1 Benchmark、回归门槛、对照报告 | Prometheus、Loki、持久化产品会话、更多资源入口、告警、自动修复 |
| 3 | 单集群丰富诊断与历史 | Prometheus、Loki、持久化会话、更多资源入口，并用 Phase 2 Benchmark 做消融对照 | 告警、自动修复 |
| 4 | 知识增强与经验检索 | 文档知识库、已验证 Incident Case、混合检索、引用与权限过滤 | 告警、修复计划和自动执行 |
| 5 | 告警自动诊断 | Alertmanager、目标解析、去重与生命周期 | 修复计划和自动执行 |
| 6 | 可审阅修复计划 | 结构化 Plan、人工修改/批准/取消 | Kubernetes 写执行 |
| 7 | 受控执行 | Action Gateway、Policy Engine、Executor、执行后验证 | 无未来 Phase 依赖 |

演进顺序解决能力依赖而不混合产品边界：先完成单集群诊断，再建立可复现的评测基线，用评测证明实时证据增强和 RAG 的净收益，然后增加触发方式，最后才进入写操作。

## 21.3 兼容原则

- `DiagnosisRequest` 保持单集群资源身份契约：`apiVersion/kind/namespace/name/uid`；本项目不设计跨集群路由。
- 当前 Phase 1 实现中若仍存在项目自定义的 `cluster_id` 字段，它只是一项待清理的历史契约，不代表继续支持多集群。设计通过后应在独立代码变更中从 AI Plugin、Agent Request/Session/Prompt 与测试中删除；Headlamp 自身用于选择当前上下文的内部集群标识不在本项目改造范围内。
- Phase 2 的评测字段和 Trace 关联字段采用向后兼容的可选扩展，不改变 Phase 1 用户调用路径。
- 新数据源以 Connector capability 形式增加；Agent 不得把 Phase 3 capability 当作 Phase 1 或 Phase 2 启动条件。
- Phase 4 的 Knowledge Reference 与 Historical Case 独立于实时 Evidence；任何后续阶段不得用检索内容覆盖实时事实。
- 告警触发复用既有 Diagnosis 创建入口，不建立第二套诊断流程。
- Remediation Plan 与 Diagnosis Result 分离；没有 Phase 7 时 Phase 6 仍可正常运行。
- Executor 是独立部署边界；Phase 1～6 不携带 Kubernetes 写权限。


---

# 22. Phase 1：单集群 Pod 人工诊断（已完成）

当前状态：Phase 1 已完成，作为 Phase 2 的被测系统与回归基线。其目标是在单个真实 Kubernetes 集群上为 Pod 提供人工触发的智能诊断能力，证明从 Headlamp 入口到诊断结果展示的完整链路可行。诊断事实仅来自 Kubernetes API，包括资源状态、Conditions、Events、Pod Logs 和必要关联资源。

## 22.1 部署拓扑

Headlamp、Agent Service 和只读 Connector 部署在同一 PoC 集群。本项目后续阶段继续保持单集群边界。

```text
用户
  │
  ▼
Headlamp Pod 详情页
  │  POST /api/v1/diagnoses
  ▼
Agent Service ──内部 Tool 调用──> Connector ──只读──> Kubernetes API
  │                                      ├─ Resource/Conditions
  │                                      ├─ Events
  │                                      ├─ Pod Logs
  │                                      └─ Owner/Node Relations
  ▼
Diagnosis Result
```

## 22.2 用户边界

- 仅 Pod 详情页提供「智能诊断」入口。
- Connector 可以读取该 Pod 的 ReplicaSet、Deployment、Node 等必要关联事实，但这些资源在 Phase 1 不提供独立诊断按钮。
- 用户提交后获得 Diagnosis ID；页面轮询状态并展示最终结构化结果。
- Phase 1 不提供聊天、追问、告警入口或修复操作。

## 22.3 最小公共契约

创建诊断：

```http
POST /api/v1/diagnoses
Content-Type: application/json
```

```json
{
  "trigger": "manual",
  "resource": {
    "apiVersion": "v1",
    "kind": "Pod",
    "namespace": "payment",
    "name": "payment-api-7b8c9",
    "uid": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

接受响应：

```json
{
  "diagnosis_id": "diag_01",
  "status": "queued"
}
```

查询诊断：

```http
GET /api/v1/diagnoses/{diagnosis_id}
```

状态仅包含 `queued`、`investigating`、`completed`、`failed`。证据不足不是系统失败：此时状态为 `completed`，`root_cause` 为空并明确说明缺少什么证据。

```json
{
  "diagnosis_id": "diag_01",
  "status": "completed",
  "result": {
    "symptom": "Pod 持续重启",
    "evidence": [
      {
        "source": "kubernetes.status",
        "observed_at": "2026-08-28T00:00:00Z",
        "summary": "Last termination reason is OOMKilled"
      }
    ],
    "root_cause": "容器内存上限不足",
    "confidence": "high",
    "recommendations": ["检查应用内存配置和容器 memory limit"]
  },
  "error": null
}
```

## 22.4 Connector 最小能力

| Tool | Phase 1 责任 | 限制 |
|---|---|---|
| `inspect` | 获取目标及必要关联资源的 metadata/spec/status/conditions | 只读；返回时校验请求 UID 与当前 UID |
| `events` | 获取目标最近 Events | 有时间和条数上限 |
| `logs` | 通过 Kubernetes Pod Logs API 获取当前/前一容器日志 | 有时间、字节和行数上限；不依赖 Loki |
| `relations` | 沿 ownerReferences 及 Pod 的 nodeName 获取必要关系 | 不做全图扫描 |

Phase 1 不要求通用 CRD 诊断，也不承诺所有 Pod 故障都能得到唯一根因。

## 22.5 状态与存储

Phase 1 使用进程内 Diagnosis Session 存储。服务重启导致历史丢失是已知限制；它不能被宣传为持久化功能。Phase 2 只持久化评测产物，不改变产品 Session 存储；Phase 3 才引入持久化产品 Session，且不迁移 Phase 1 的临时历史。

## 22.6 验收场景

真实测试集群至少注入并验证：

1. OOMKilled：状态、上一容器终止原因和日志能够支持根因。
2. ImagePullBackOff：Container State 与 Events 能够支持根因。
3. FailedScheduling：Pending 状态与调度 Events 能够支持根因或证据不足声明。

验收要求从 Headlamp 点击开始，到页面出现结构化结果结束；直接调用 Agent 或 Connector 的测试不等于端到端通过。整条链路不使用 Mock，也不依赖后续 Phase。

## 22.7 非目标与运行依赖

非目标：Phase 1 不处理告警自动触发和自动修复；不把认证、安全加固、Prompt Injection 防护等生产化问题作为启动或验收前置条件。

运行依赖：Headlamp、Agent Service、只读 Connector 与一个真实 Kubernetes 集群。未安装 Prometheus 和 Loki 时，用户仍能完成 Pod 人工诊断闭环，系统不会因缺少评测、告警、认证、安全加固或自动修复能力而无法启动。

## 22.8 失败边界

| 失败模式 | Phase 1 行为 |
|---|---|
| 目标 UID 与当前对象不一致 | 拒绝诊断并提示资源已重建 |
| Connector 或 Kubernetes API 不可用 | Session `failed`，返回可定位错误 |
| LLM 超时或返回非法结构 | 有限重试后 `failed`，保留已收集证据摘要 |
| 证据不足 | `completed`，不虚构根因，列出缺失证据 |


---

# 23. Phase 2：Agent 评测与能力改进

目标：在不扩展 Phase 1 产品能力边界的前提下，把“单集群 Kubernetes-only Agent 到底能做到什么程度”变成可重复、可比较、可回归的工程事实。Phase 2 的独立用户可见产物是一份可追溯的 Benchmark Report；它必须指出每个 Case 的结论、证据、失败层、耗时与成本，而不只是给出一个总分。

Phase 2 把 Phase 1 已完成链路视为被测系统。它增加基础可观测性、Eval Harness、正式故障 Case、确定性评分、重复运行、基线冻结和候选版本对照，不增加 Prometheus、Loki、持久化产品 Session 或新的资源诊断入口。

## 23.1 交付边界

Phase 2 必须交付：

- 在首次冻结基线前完成单集群契约对齐：移除项目自定义 `cluster_id`，重新通过 Phase 1 真实端到端验收；本次仅修改设计，不实施该代码变更。
- 10～15 个首批正式 Case，覆盖 OOMKilled、ImagePullBackOff、FailedScheduling、CrashLoop、配置错误、健康 Pod 与证据不足；每个 Case 可独立注入、判稳和清理。
- 基础 Trace，能够按 `diagnosis_id` 关联 Agent、Tool、Connector 和 Kubernetes API 的可观测执行行为。
- Eval Harness：Case Loader、Fault Injector、Runner、Trace Collector、Scorer、Reporter。
- Phase 1 Baseline：固定 Case 版本、环境版本、模型、Prompt、Tool Schema 与运行参数后，每个 Case 至少重复 5 次。
- 候选版本与冻结基线的成对对照报告，以及把失败 Case 收入回归集的能力改进闭环。
- Phase 1 真实 Headlamp 端到端场景继续通过，证明评测设施没有破坏既有用户链路。

Phase 2 不交付：

- Prometheus/Loki 诊断能力、Deployment/Node/PVC 新入口或产品 Session 持久化；这些属于 Phase 3。
- 在线自动学习、自动改 Prompt、自动换模型或自动发布候选版本。
- 以 LLM 主观打分代替可验证 Ground Truth，或由被测模型给自己的答案评分。
- 告警触发、修复计划和 Kubernetes 写操作。

## 23.2 评测架构

```text
                       Case Repository
                  manifest + ground truth
                             │
                             ▼
                         Eval Runner
                  ┌──────────┼──────────┐
                  │          │          │
                  ▼          ▼          ▼
            Fault Injector  Agent    Trace Collector
                  │          │          │
                  ▼          ▼          │
             kind / K8s   Connector─────┘
                  │          │
                  └──────┬───┘
                         ▼
                Result + Ground Truth + Trace
                         │
                         ▼
                 Deterministic Scorer
                         │
                         ▼
              JSON Report + Markdown Report
```

建议目录边界：

```text
eval/
├── cases/                 # Case 定义、故障清单、Ground Truth
├── injector/              # 确定性的 apply/wait/cleanup
├── runner/                # 调 Agent、重复运行、超时与隔离
├── scorer/                # 规则评分与可选盲审适配器
├── reports/               # 不提交的运行产物或发布基线
└── suites/                # Case 集合、预算与发布门槛
```

## 23.3 Case 契约

Case 必须版本化，且把故障注入、判稳、Ground Truth 和清理写在同一份声明中。最小示例：

```yaml
schema_version: eval.k8spilot.io/v1alpha1
id: pod-oomkilled-001
suite: phase1
description: Container exceeds its memory limit and is terminated
target:
  apiVersion: v1
  kind: Pod
  namespace: eval-phase1
  name: oom-demo
setup:
  manifests:
    - manifests/oom-demo.yaml
  preflight:
    - type: namespace_empty
inject:
  action: apply
ready_when:
  type: jsonpath_equals
  path: status.containerStatuses[0].lastState.terminated.reason
  value: OOMKilled
  timeout_seconds: 120
ground_truth:
  accepted_root_cause_codes: [CONTAINER_OOMKILLED]
  required_evidence:
    - source: kubernetes.status
      path: status.containerStatuses[0].lastState.terminated.reason
      operator: equals
      value: OOMKilled
  abstention_expected: false
budgets:
  diagnosis_timeout_seconds: 60
  max_tool_calls: 12
cleanup:
  action: delete_manifest
```

硬约束：

- Ground Truth 只能来自 Case 定义和真实集群观测，不能从 Agent 自由文本反推。
- Case 的 `ready_when` 未满足时记为 `fixture_failed`，不得把它计为 Agent 诊断错误。
- `cleanup` 必须在成功、失败、超时和中断路径执行；清理失败要响亮暴露并阻止下一个可能冲突的 Case。
- Case 默认使用独立 Namespace；同一 Case 的并发运行必须使用唯一运行后缀，避免资源互相污染。
- Case 变更会产生新的 `case_version`；不得用更新后的 Case 覆盖旧基线并继续声称结果可比。

## 23.4 可评分的诊断结果

Phase 2 为现有 `DiagnosisResult` 增加向后兼容的结构化字段，原有展示字段保留：

```json
{
  "diagnosis_id": "diag_01",
  "status": "completed",
  "result": {
    "symptom": "Pod 持续重启",
    "root_cause_code": "CONTAINER_OOMKILLED",
    "root_cause": "容器超过 memory limit 后被终止",
    "evidence": [
      {
        "source": "kubernetes.status",
        "resource_uid": "550e8400-e29b-41d4-a716-446655440000",
        "path": "status.containerStatuses[0].lastState.terminated.reason",
        "operator": "equals",
        "value": "OOMKilled",
        "observed_at": "2026-08-28T00:00:00Z",
        "summary": "Last termination reason is OOMKilled"
      }
    ],
    "confidence": "high",
    "recommendations": ["检查应用内存配置和容器 memory limit"]
  }
}
```

`root_cause_code` 使用版本化枚举；证据使用 `source/resource_uid/path/operator/value/observed_at` 做确定性匹配。自由文本 `root_cause` 和 `summary` 用于用户理解，不作为第一优先级评分输入。证据不足时 `root_cause_code` 和 `root_cause` 均为空，并显式返回 `insufficient_evidence` 与缺失证据列表。

## 23.5 Runner 生命周期与命令

最小命令：

```bash
./eval run --suite phase1 --runs 5 --profile baseline
./eval compare --baseline <run-id> --candidate <run-id>
```

每次 Case 运行严格执行：

```text
环境预检
→ 创建隔离 Namespace/资源
→ 等待 ready_when
→ 创建 Diagnosis
→ 轮询到终态并收集 Trace
→ 读取真实集群终态
→ 确定性评分
→ 清理资源并验证清理完成
→ 写入不可变 Case Result
```

Runner 不负责选择 Prompt、模型或自动重试失败 Case。模型调用层自己的有限重试计入同一次运行并写入 Trace；Runner 只按 Suite 声明重复运行，避免用“重跑直到成功”抬高结果。

## 23.6 Trace 与实验身份

一次 Diagnosis 是一条 Root Span。最小 Span 树：

```text
diagnosis
├── llm.call
├── tool.inspect ── connector.inspect ── kubernetes.get
├── tool.events  ── connector.events  ── kubernetes.list
├── tool.logs    ── connector.logs    ── kubernetes.pod_logs
└── llm.final
```

每个 Eval Run 固定并记录：

- `eval_run_id`、`case_id`、`case_version`、`attempt_index`、`diagnosis_id`。
- `agent_version`、镜像摘要、`prompt_version`/Prompt Hash、`tool_schema_version`。
- 模型提供方、模型名、推理参数与采样参数。
- Kubernetes 版本、Case Fixture 版本和相关组件版本。
- LLM 输入/输出 Token、每次 Tool 的名称/参数摘要/状态/耗时/结果大小/是否截断、总诊断耗时。

默认不把完整 Prompt、Secret 或原始日志写入 Trace；需要保存诊断输入时先脱敏，并使用受控的 Eval Artifact 存储。记录的是可观测执行行为和结构化决策结果，不记录隐藏思维链。

## 23.7 评分与报告

语义质量指标：

- `Root Cause Accuracy = 根因码正确的可评分运行数 / 可评分运行数`。
- `Wrong Root Cause Rate = 给出错误根因的运行数 / 全部有效运行数`。
- `Abstention Accuracy = 证据不足 Case 中正确拒答的运行数 / 证据不足 Case 有效运行数`。
- `Evidence Recall = 匹配到的必需证据数 / Ground Truth 必需证据总数`。
- `Evidence Precision = 可由 Ground Truth 验证的返回证据数 / 返回证据总数`；不可验证的自由文本证据单独列出，不默认判真。

运行质量指标：

- Diagnosis Success Rate、Fixture Failure Rate、结构化输出合法率。
- 平均值及 P50/P95 Diagnosis Duration、Tool Calls、LLM Calls、Token Usage。
- Duplicate Tool Call Rate、Tool Error Rate、日志截断率和重试次数。

报告至少输出 `run.json`、逐 Case 的 `case-results.jsonl`、机器可读 `report.json` 和用户可读 `report.md`。总分不能掩盖分组结果；必须按故障类型、Case、运行次数列出均值、波动、失败层和证据缺口。`fixture_failed`、`system_failed` 与 `diagnosis_incorrect` 分开统计。

如果结构化规则无法覆盖同义根因，可以增加独立盲审：评审输入不得包含候选版本名，Judge 模型和 Prompt 必须固定并版本化，先在人工标注小集上校准。Judge 结果只能作为独立维度，不能覆盖确定性评分。

## 23.8 基线、对照与能力改进闭环

第一次 Phase 1 Benchmark 的目标是建立事实基线，不为准确率预设一个缺乏数据依据的漂亮数字。Suite 先定义结构性门槛：所有 Fixture 可重复建立和清理、所有有效运行均有结果与 Trace、报告可复算、Phase 1 三个真实端到端场景继续通过。

基线冻结后，任何 Prompt、模型、Tool Schema、上下文裁剪或规则变化都作为单变量候选实验：

```text
冻结 Baseline
→ 从失败 Trace 定位失败层
→ 提出一个可证伪的改动假设
→ 在相同 Case/环境/重复次数上运行 Candidate
→ 生成逐 Case 成对差异
→ 通过门槛后合入，否则拒绝
→ 新暴露的失败 Case 加入回归集
```

候选版本至少满足：关键 Case 无回归、Wrong Root Cause Rate 不上升、结构化输出合法率不下降，并满足 Suite 中预先声明的耗时/Tool/Token 预算。若质量与成本门槛冲突，报告必须同时暴露，由人选择一个版本；不得把两套冲突行为混成一个不可解释的折中实现。

Phase 3 引入 Prometheus/Loki 时，必须复用本阶段冻结的 Kubernetes-only Benchmark，运行 `K8s`、`K8s+Prometheus`、`K8s+Loki`、`K8s+Prometheus+Loki` 消融矩阵，证明每个数据源在哪些 Case 上带来净收益。

## 23.9 失败边界

| 失败模式 | Phase 2 行为 |
|---|---|
| Fixture 未达到故障稳态 | 标记 `fixture_failed`，不计入 Agent 准确率，清理后停止或按 Suite 策略继续 |
| Agent/Connector/Kubernetes API 失败 | 标记 `system_failed` 并保留 Trace，不伪装成根因错误 |
| 结果不符合 Schema | 标记结构化输出失败；保存原始响应的脱敏摘要用于定位 |
| Trace 缺失或无法按 Diagnosis ID 关联 | 本次结果不可作为可发布基线，评测运行失败 |
| 清理失败 | 标记运行失败并阻止有污染风险的后续 Case |
| 报告元数据不完整 | 禁止与既有基线比较，不发布“提升”结论 |

## 23.10 验收场景

1. 一条命令可在干净的单集群环境中完成 Case 注入、判稳、诊断、Trace 收集、评分、清理和报告生成，无人工改结果。
2. 首批 10～15 个 Case 每个至少运行 5 次；报告能区分 Agent 错误、系统错误和 Fixture 错误，并能从 `diagnosis_id` 定位完整 Tool 路径。
3. 同一份 `case-results.jsonl` 可离线重算出相同 `report.json`；Case 或评分器版本变化后不会覆盖旧结果。
4. 对一个受控候选改动生成 Baseline/Candidate 成对报告，明确列出改善、回归、无变化、成本和延迟变化。
5. Phase 1 的 OOMKilled、ImagePullBackOff、FailedScheduling 仍从 Headlamp 点击到结构化结果做真实端到端回归；直接调用 Agent、Mock Connector 或仅生成 Eval Report 不能替代该产品验收。


---

# 24. Phase 3：单集群丰富诊断与历史

目标：在 Phase 1 单集群人工诊断与 Phase 2 冻结 Benchmark 均保持可用的前提下，增加 Prometheus、Loki、持久化 Diagnosis Session、历史查询和更多资源入口，并用可重复实验说明新增能力的实际贡献。

Phase 3 保留 Kubernetes-only 路径，并增加可选 Connector capabilities：`prometheus.metrics` 和 `loki.logs`。Agent 先查询 capabilities，再决定是否调用；数据源不可用、超时或返回截断时必须记录在结果中并继续使用已有证据。

## 24.1 新增用户能力

- Prometheus 指标证据和 Loki 日志证据。
- Diagnosis Session 持久化及重启后历史查询。
- 调查步骤和 Tool 调用进度展示。
- Deployment、Node、PVC 的人工诊断入口。

## 24.2 运行依赖与降级

Phase 3 交付时，未安装 Prometheus/Loki 的环境仍必须通过 Phase 1 验收和 Phase 2 Kubernetes-only Benchmark。任一扩展数据源不可用时，Agent 回退到 Phase 1 的 Kubernetes 事实诊断，并在结果中明确标识。持久化存储的数据模型、保留期限和具体数据库由 Phase 3 独立设计定义。

## 24.3 评测要求

- Phase 2 Benchmark 不得被替换或修改后冒充原基线；新增 Case 以新 Suite 版本累积。
- 对 Prometheus/Loki 运行四组消融实验，模型、Prompt、Case、重复次数和 Kubernetes 环境保持一致。
- 报告必须按故障类型说明准确率、错误根因率、证据召回、Tool/Token/耗时变化，不能只证明“数据源已接入”。
- 任一扩展数据源的加入不得让 Phase 1 关键 Case 回归；若发生冲突，保留 Kubernetes-only 路径并阻止发布。

## 24.4 非目标与失败边界

Phase 3 不包含告警自动触发和自动修复；单集群人工诊断与评测路径保持可用。Prometheus/Loki 不可用、超时或返回截断时，记录在结果与 Trace 中并回退 Kubernetes-only 路径，不影响 Phase 1 能力或历史查询。

## 24.5 验收场景

1. 可观测数据源增强诊断：Prometheus 或 Loki 能力可用时，Agent 可以按需使用对应证据增强诊断；任一扩展数据源不可用时仍能降级到 Phase 1 的 Kubernetes 事实诊断。
2. 诊断历史跨重启保留：Agent Service 重启后，已完成的 Diagnosis Session 及结构化结果仍可查询。
3. 生成四组消融对照报告，能够说明 Prometheus、Loki 分别对哪些 Case 有正向、负向或无影响。


---

# 25. Phase 4：知识增强与历史经验检索

目标：在 Phase 3 已具备实时 Kubernetes、Metrics、Logs 和持久化 Diagnosis Session 的基础上，引入专业知识 RAG 与历史 Incident Case Retrieval，帮助 Agent 形成更好的故障假设、选择调查路径并给出有来源的处理建议。检索内容只提供辅助上下文，不能替代实时事实或直接决定 Root Cause。

Phase 4 的独立用户结果是：诊断页在保留实时 Evidence 的同时，新增“参考知识”和“相似历史 Incident”分区，用户可以看到来源、适用版本、更新时间、相似原因、历史处置和验证结果，并能打开原始文档或 Incident。

## 25.1 三类信息与固定优先级

Agent 使用三类信息：

1. **实时事实**：Kubernetes Status/Conditions/Events、当前配置与资源关系、Prometheus Metrics、Loki Logs。通过 Connector Tool 获取，是诊断 Root Cause 的主要证据。
2. **专业知识**：Kubernetes 官方文档、产品文档、Runbook、SOP、故障处理手册、已知问题库和架构设计文档。通过 Knowledge Retrieval 获取，用于解释事实、提出假设和选择下一步 Tool。
3. **历史经验**：已经关闭并验证结论的历史 Incident，包括当时的 Evidence、Root Cause、处置步骤、验证结果和相关版本。通过 Incident Retrieval 获取，用于寻找相似模式。

冲突时使用固定优先级：

~~~text
实时 Tool Evidence
    > 当前环境事实与配置
    > 已验证历史 Incident
    > Runbook / Knowledge Base
    > 模型自身知识
~~~

该顺序是系统不变量，不由模型临时决定：

- RAG 命中只能增加或降低某个假设的调查优先级，不能直接把假设升级为 Root Cause。
- Root Cause 至少要有一条当前诊断产生的实时 Evidence 支撑；只有文档或历史 Case 命中时必须继续调查或声明证据不足。
- 检索结果与实时 Evidence 冲突时，保留冲突记录并以实时 Evidence 为准。
- 模型自身知识不得覆盖任何有来源、版本和时间的事实或检索内容。

## 25.2 架构与职责边界

~~~text
文档源 / Runbook / SOP / 已知问题
                │
                ▼
       Knowledge Ingestion Pipeline
     parse → normalize → chunk → validate
                │
                ▼
      Metadata Store + Retrieval Index
                │
                │ search_knowledge
                ▼
Agent Service ────────> Knowledge & Experience Service
     │                              ▲
     │ search_incidents             │
     │                              │
     └──────────────> Verified Incident Case Store
     │
     │ inspect / relations / events / logs / metrics
     ▼
Connector ───────────> Kubernetes / Prometheus / Loki
~~~

职责边界：

- Agent 负责根据实时事实构造检索意图、综合上下文和继续调查。
- Knowledge & Experience Service 负责权限过滤、版本过滤、混合检索、重排、引用和上下文预算，不负责诊断结论。
- Connector 继续只负责当前环境事实，不读取知识库，也不调用 LLM。
- Ingestion Pipeline 只处理已登记的数据源；检索请求不能临时抓取任意 URL。
- 文档知识库与 Incident Case Store 逻辑隔离，分别评测和控制生命周期。

## 25.3 强类型检索 Tool

Agent 只能调用受约束的强类型 Tool，不直接访问向量数据库：

~~~json
{
  "tool": "search_knowledge",
  "arguments": {
    "query": "容器因 OOMKilled 重启时应检查哪些限制",
    "filters": {
      "source_types": ["runbook", "product_doc", "known_issue"],
      "product": "payment-api",
      "versions": ["2.4.1"],
      "environment": "production"
    },
    "top_k": 5
  }
}
~~~

~~~json
{
  "tool": "search_incidents",
  "arguments": {
    "signals": {
      "resource_kind": "Pod",
      "symptoms": ["CrashLoopBackOff", "OOMKilled"],
      "root_cause_candidates": ["CONTAINER_OOMKILLED"],
      "product": "payment-api",
      "version": "2.4.1"
    },
    "top_k": 5
  }
}
~~~

共同限制：

- top_k、单段字符数和总上下文 Token 有硬上限。
- 先执行 ACL、产品、版本、环境、有效期等确定性过滤，再执行关键词/向量混合召回和重排。
- Tool 返回稳定 retrieval_id，Trace 记录查询摘要、过滤条件、候选数、命中数、耗时和引用 ID，但默认不记录完整敏感正文。
- Agent 对同一假设和过滤条件不得重复检索；除非新增实时 Evidence 改变了检索条件。

## 25.4 Knowledge Document 契约与摄取生命周期

每份知识文档至少包含：

~~~yaml
document_id: kb-payment-oom-001
source_type: runbook
title: Payment API OOM 处理手册
source_uri: https://docs.example/runbooks/payment-oom
product: payment-api
versions: [2.4.x]
environments: [production, staging]
owner: payment-platform
valid_from: 2026-01-01T00:00:00Z
valid_until: null
checksum: sha256:...
acl_tags: [team:payment, role:sre]
~~~

摄取状态机：

~~~text
discovered → parsed → validated → indexed → active
                      │              │
                      └→ quarantined └→ superseded / deleted
~~~

规则：

- 文档按 document_id + checksum 版本化；新版本生效后旧版本标记 superseded，不得静默覆盖。
- Chunk 必须保留 document_id、章节路径、原始 URI、版本、有效期和 ACL，确保引用可以返回原文位置。
- 删除或撤权必须同步删除检索索引中的可见性，不能只删除元数据。
- 过期、解析失败、来源不可信或包含高风险指令的内容进入隔离区，不进入生产检索。
- Phase 4 首批只接入明确授权的静态文档源；具体向量库、Embedding 模型和对象存储由独立实现设计选择。

## 25.5 历史 Incident Case 契约

只有已经关闭、Root Cause 经人工确认且修复结果已验证的 Incident 才能进入可检索 Case Store。最小字段：

~~~yaml
incident_id: inc-2026-00128
status: verified
product: payment-api
product_version: 2.4.1
environment: production
resource_kind: Pod
symptoms: [CrashLoopBackOff, OOMKilled]
evidence_signature:
  - source: kubernetes.status
    path: status.containerStatuses[*].lastState.terminated.reason
    value: OOMKilled
root_cause_code: CONTAINER_OOMKILLED
remediation_summary: Increase memory limit after capacity review
verification:
  outcome: success
  verified_at: 2026-08-31T10:00:00Z
~~~

未关闭、根因未确认、修复失败或缺少验证结果的 Incident 可以保留在历史系统中，但不得作为“已验证经验”返回给 Agent。Case 更新必须保留修订记录；模型生成的总结不能覆盖原始 Evidence、人工结论和验证结果。

## 25.6 检索结果与 Diagnosis Result 契约

检索结果统一返回：

~~~json
{
  "retrieval_id": "ret_01",
  "type": "knowledge",
  "score": 0.86,
  "content": "检查容器 memory limit 与历史峰值是否匹配。",
  "citation": {
    "document_id": "kb-payment-oom-001",
    "title": "Payment API OOM 处理手册",
    "source_uri": "https://docs.example/runbooks/payment-oom",
    "section": "2.1 内存限制",
    "version": "2.4.x",
    "updated_at": "2026-08-20T00:00:00Z"
  }
}
~~~

DiagnosisResult 必须把三类信息分开：

~~~text
evidence[]              当前环境实时证据，可支撑 Root Cause
historical_cases[]      相似历史经验，只能辅助假设和建议
knowledge_references[]  文档知识引用，只能辅助解释和建议
~~~

每条历史 Case 或知识引用应说明 used_for：hypothesis、investigation、explanation 或 recommendation。不得标记为 root_cause_evidence。用户界面必须在视觉和文案上区分“当前证据”与“参考资料”。

## 25.7 Agent 调查策略

推荐调查顺序：

~~~text
收集最小实时事实
→ 形成候选假设
→ 仅在存在知识缺口时检索文档或历史 Case
→ 根据命中结果选择下一步实时 Tool
→ 用新增实时 Evidence 验证或否定假设
→ 输出 Root Cause、实时 Evidence 和独立参考引用
~~~

例如历史 Case 认为 CrashLoopBackOff 常由数据库连接池错误引起，但当前实时证据为 exitCode=137、reason=OOMKilled 且内存接近 Limit，Agent 必须优先判断内存限制问题；历史 Case 只能作为被否定或低优先级假设记录。

## 25.8 安全、权限与失败边界

- 检索文档、Incident 内容和原始日志都视为不可信数据；其中的“忽略规则”“执行命令”“调用某 Tool”等文本不得改变系统指令、Tool 权限或证据优先级。
- 查询前根据当前用户身份执行文档和 Incident ACL 过滤；未授权内容不得进入候选集、Trace、Prompt 或引用。
- 对查询文本、返回内容和引用元数据设置长度限制并脱敏 Secret、Token、个人信息和内部凭证。
- Knowledge Service 不可用、超时或无命中时，Diagnosis 标记 knowledge_degraded 或 no_knowledge_match，继续使用 Phase 3 实时事实完成诊断。
- 引用对应的文档已删除、过期或版本不适用时，不得返回该内容；无法确认适用版本时显式降低可信度。
- 检索结果不能触发 Kubernetes 写操作；Phase 4 仍保持全链路只读。

## 25.9 评测与消融

Phase 4 必须扩展 Phase 2 Eval Harness，而不是只验证“向量库能查到内容”：

- Retrieval：Recall@K、MRR/nDCG、无关命中率、过期文档命中率、ACL 泄漏数、检索 P95。
- Citation：引用可打开率、引用内容支持陈述的比例、版本/章节正确率。
- Diagnosis：Root Cause Accuracy、Wrong Root Cause Rate、Evidence Recall、Abstention Accuracy、Tool/Token/耗时。
- Priority：实时 Evidence 与知识冲突时的事实优先通过率。
- History：相似 Incident 命中率、错误版本 Case 排除率、未验证 Incident 泄漏数。

运行四组同条件消融：

| 实验 | 实时事实 | 文档 RAG | Incident Retrieval |
|---|---|---|---|
| A | ✓ | ✗ | ✗ |
| B | ✓ | ✓ | ✗ |
| C | ✓ | ✗ | ✓ |
| D | ✓ | ✓ | ✓ |

模型、Prompt、实时数据、Case 和运行次数保持一致。Phase 4 只有在关键 Case 无回归、错误根因率不升高、冲突 Case 坚持实时事实且引用质量达到 Suite 门槛时才能发布。

## 25.10 非目标与验收场景

非目标：

- 不做模型微调，不让 Agent 自动修改生产知识库。
- 不把所有 Diagnosis 自动沉淀为可信经验；只有人工确认且验证成功的 Incident 才可进入 Case Store。
- 不接入任意互联网搜索或未登记 URL。
- 不因检索到 Runbook 就自动执行其中的命令；修复计划与写操作仍属于 Phase 6 和 Phase 7。

验收至少覆盖：

1. **文档知识命中**：产品特殊限制或已知问题 Case 能返回正确版本和章节引用，并引导 Agent 调用正确的下一步实时 Tool。
2. **历史经验命中**：相似 Incident 返回原始 Evidence 摘要、Root Cause、修复和验证结果，且不会被当作当前 Evidence。
3. **事实冲突**：历史 Case 指向数据库配置，但当前状态明确为 OOMKilled 时，最终 Root Cause 以实时 Evidence 为准，并记录冲突。
4. **版本与新鲜度**：旧版本或过期 Runbook 不进入有效候选；无适用版本时明确说明。
5. **权限隔离**：无权限用户无法通过检索结果、引用或 Trace 得到受限文档和 Incident 内容。
6. **降级运行**：Knowledge Service 完全不可用时，Phase 3 的人工诊断、历史查询和实时证据链仍可独立完成。
7. **端到端展示**：Headlamp 页面分别展示实时 Evidence、相似 Incident 和知识引用，用户可以追溯来源且不会误认为三者权重相同。


---

# 26. Phase 5：告警自动诊断

目标：在人工诊断继续可用的基础上，接入 Alertmanager，实现告警目标解析、自动诊断、去重、恢复和重新触发。

## 26.1 运行依赖

增加 Alertmanager 触发适配，但复用 `DiagnosisRequest`、Agent Loop、Connector Tool 和 Diagnosis Result。告警先归一化为包含目标资源、fingerprint、startsAt、状态和原始标签摘要的请求。

## 26.2 去重与生命周期

- 去重键为 `fingerprint`。
- 同一告警实例在 firing 生命周期内重复到达时，系统更新既有 Diagnosis Session 而不重复创建并行诊断。
- resolved 关闭当前生命周期；之后重新 firing 创建新的诊断生命周期。
- 无法唯一映射资源的告警进入 `unresolved_target`，不得猜测目标。

## 26.3 非目标

Phase 5 不生成修复计划，也不执行 Kubernetes 写操作；人工诊断路径保持可用。

## 26.4 失败边界

Alertmanager 或目标解析失败不影响人工诊断；告警风暴进行限流。

## 26.5 验收场景

重复告警合并诊断：同一告警实例在 firing 生命周期内重复到达时，系统更新既有 Diagnosis Session 而不重复创建并行诊断；告警恢复后再次 firing 可以创建新的诊断生命周期。Phase 5 独立验收覆盖重复 firing、resolved、再次 firing 和告警风暴限流。


---

# 27. Phase 6：修复计划

目标：根据诊断结果生成结构化 Remediation Plan，并支持人工查看、修改、批准或取消，但不得执行 Kubernetes 写操作。

## 27.1 运行依赖

基于 Diagnosis Result 生成独立的结构化 Remediation Plan。Plan 至少包含目标资源身份、步骤、前置条件、风险、预期影响和验证方法，并支持草稿、已审阅、已批准、已取消状态。

Plan 示例：

    Root Cause:
    payment-api Memory Limit 过低

    Plan:
    Step 1  将 memory limit 2Gi -> 3Gi
    Step 2  Rolling Restart
    Step 3  等待 5 分钟
    Step 4  检查 RestartCount / P99 / Error Rate

Phase 6 只生成 Plan，不自动执行；用户可以 Review、Modify、Cancel。

## 27.2 非目标

Phase 6 不部署 Executor，也不给现有组件增加 Kubernetes 写权限；批准只表达人工意图，不触发执行，即批准计划不触发 Kubernetes 写操作。没有 Phase 7 时，诊断与计划功能仍完整可用。

## 27.3 失败边界

Plan 状态为草稿、已审阅、已批准、已取消；取消或修改不影响诊断历史。任何情况下不对 Kubernetes 资源执行 create、update、patch 或 delete。

## 27.4 验收场景

计划不触发执行：用户生成或批准修复计划时，系统保存计划及人工决策，不对 Kubernetes 资源执行 create、update、patch 或 delete。


---

# 28. Phase 7：受控执行

目标：通过 Action Gateway、Policy Engine 和独立 Executor 执行已批准的结构化动作，并在步骤失败时停止后续执行和验证结果。

## 28.1 运行依赖

引入 Action Gateway、Policy Engine 和独立 Executor。Agent 不能提交 shell 命令，只能提交受版本控制的结构化 Action。Executor 的权限、短期凭证和回滚策略由 Phase 7 独立设计细化。

执行架构：

    Agent
      |
      v
    Action Gateway
      |
      v
    Policy Engine
      |
      v
    Executor
      |
      v
    Kubernetes API

组件职责：

    Executor        真正执行 Kubernetes 修改；Agent 不直接执行 kubectl
    Action Gateway  提供结构化 Action：restart_workload / scale_workload / patch_resource，
                    而不是 execute_shell(command)
    Policy Engine   判断「谁 / 对什么资源 / 做什么操作 / 是否允许」；
                    例如 production namespace 上 delete deployment 返回 DENY

## 28.2 执行前置校验

执行前校验人工批准、策略、目标 UID/resourceVersion 和前置条件。

## 28.3 非目标

本 Phase 不扩大 Agent 推理或诊断能力；未批准、未通过策略的动作一律不执行。

## 28.4 失败边界

计划逐步执行；任一步骤失败或执行后验证失败时停止后续步骤。拒绝、执行、失败和验证结果均进入审计记录。

## 28.5 验收场景

1. 策略拒绝写操作：已批准计划中的动作不满足策略时，Executor 不执行该动作，系统记录拒绝原因和审计结果。
2. 步骤失败停止计划：任一执行步骤失败或执行后验证失败时，当前计划进入失败状态，后续步骤不再执行。


---

# 29. Short-lived Token（Phase 7 细化）

Phase 7 不建议 Executor 长期持有高权限 kubeconfig。

建议：

    Executor
       |
       v
    Credential Broker
       |
       v
    Short-lived Token
       |
       v
    Kubernetes API


例如 Token：

    TTL = 5 ~ 15 min


即使泄露：

    风险窗口有限


---

# 30. Plan State Machine（Phase 7 细化）

生产自动修复不能：

    Agent 输出3个命令
       |
       v
    Executor全部执行


应该：

    Plan
      |
      v

    Step 1
    pending
      |
    running
      |
    done

      |
      v

    Step 2
    pending
      |
    running
      |
    failed

      |
      X

    Step 3 不再执行


状态：

    pending
    running
    done
    failed
    canceled


避免：

    前一步失败
    Agent仍继续执行后续操作


---

# 31. 可以借鉴的开源 AIOps 项目

## K8sGPT

最值得参考：

    Deterministic Analyzer

    Structured Finding

设计思想：

    确定性判断放在 LLM 前面

适合参考：

- Kubernetes Status 处理
- Conditions
- Events
- 常见故障知识

但不建议照搬：

    每种 Resource 一个 Analyzer


---

## KubeAstra

最值得参考：

    Tool Registry

    ReAct Investigation Loop

    Prometheus Integration

    Tool Result Summarization

    Remediation Plan

    Plan State Machine

    Dry Run

    Confirmation


特别值得借鉴：

    Tool 是稳定接口

    Agent 只负责编排


---

## AWS AIOps Sherlock

最值得参考：

    Prompt Contract

    Agent Evaluation

    OpenTelemetry Tracing

    Agent 调查策略


Prompt 不应该只是：

    You are Kubernetes Expert


而应该约束：

    必须先收集证据

    不能只根据单一日志判断根因

    优先低成本工具

    不重复查询

    Evidence 不足需要声明

    输出必须包含：

      symptom

      evidence

      hypothesis

      root_cause

      confidence

      recommendation


---

# 32. Agent 可观测性

Phase 1 可以只保留既有运行日志，但 Phase 2 必须补齐能支撑评测的基础 OpenTelemetry Trace。可观测性和评测的职责不同：

```text
Evaluation    回答 Agent 好不好、哪个版本更好
Observability 回答一次成功或失败具体发生在哪一层
```

Phase 2 最小链路：

```text
Eval Runner
    │ eval_run_id / case_id
    ▼
Agent Service ── traceparent ──> Connector ──> Kubernetes API
    │                              │
    └──────────── OTLP/JSONL ──────┘
                   │
                   ▼
             Eval Artifact Store
```

生产化最终形态可以将同一 OTLP 数据发送到 OpenTelemetry Collector，再分别落入 Tempo/Jaeger、Prometheus 和 Loki，并由 Grafana 展示。Phase 2 不以部署完整观测平台为验收前提；它要求的是 Trace 完整、可关联、可导出和可随报告保存。

一次 Diagnosis 对应一个 Root Span，并至少记录：

- `diagnosis_id`、`eval_run_id`、`case_id`、Agent/Prompt/Tool Schema/Model 版本。
- LLM Call 的延迟、Token、重试、Finish Reason 和结构化输出是否合法。
- Tool Call 的工具名、参数摘要、状态、耗时、返回大小、是否截断和错误类型。
- Connector/Kubernetes API 的操作类型、资源身份、延迟、状态和超时。
- Diagnosis 总耗时和最终状态。

Trace 必须区分以下失败层：

```text
fixture
agent_planning
llm_transport
tool_arguments
connector
kubernetes_api
result_schema
scoring
```

不记录隐藏思维链；默认不记录完整 Prompt、Secret、Authorization Header 或原始 Pod 日志。资源字段和日志摘要应按白名单采集，并配置大小限制与脱敏规则。

生产 Metrics 可聚合成功率、P50/P95 耗时、Tool 错误率、Token、截断率等运行事实；Root Cause Accuracy、Evidence Recall 等需要 Ground Truth 的指标只能由 Eval Runner 计算，不能伪装成线上 Prometheus 指标。


---

# 33. Evaluation

Phase 2 的详细实现契约见「23. Phase 2：Agent 评测与能力改进」。本节定义跨阶段不变的评测原则。

评测证据分为三层，不能相互替代：

1. 组件测试：验证 Case Loader、Injector、Scorer、Schema 与 Reporter 的确定性行为。
2. 能力 Benchmark：在真实 Kubernetes 故障上重复调用 Agent，衡量根因、证据、拒答、成本和延迟。
3. 产品端到端验收：从 Headlamp 用户入口开始，到页面展示结构化结果结束；它证明产品链路可用，但样本量通常不足以替代 Benchmark。

评测必须同时回答：

- 结论是否正确：Root Cause Accuracy、Wrong Root Cause Rate、Abstention Accuracy。
- 证据是否充分：Evidence Recall、Evidence Precision、不可验证证据数量。
- 调查是否有效：Tool Call Count、Duplicate Tool Call Rate、Tool Error Rate。
- 成本和性能是否可接受：Token Usage、LLM Calls、Diagnosis Duration 的均值与 P50/P95。
- 系统是否稳定：Diagnosis Success Rate、Schema Valid Rate、Fixture Failure Rate、跨重复运行波动。

任何“能力提升”结论必须满足以下可比性条件：

- 相同 Case ID 与 Case Version、相同故障稳态和 Kubernetes 环境。
- 相同重复次数；失败运行不得丢弃或用额外成功运行替换。
- 除被研究变量外，模型参数、Prompt、Tool Schema 和组件版本保持固定。
- 报告同时展示质量、错误根因、成本和延迟，不用单一总分隐藏退化。
- 原始 Case Result 与版本元数据可追溯，能够离线复算报告。

测试集按阶段累积：Phase 3 及后续阶段必须继续运行冻结的 Phase 1 Benchmark。新数据源或新资源类型只能增加新 Suite/Case，不能修改旧 Case 后继续沿用旧基线名称。


---

# 34. 当前推荐最终架构

以下为最终形态：Phase 1 包含 Headlamp、Agent Service、Connector 与 Kubernetes API；Phase 2 增加独立 Eval/Observability Plane；Prometheus/Loki 为 Phase 3 可选能力；Phase 4 增加 Knowledge & Experience Service；告警链路为 Phase 5 引入。所有阶段保持单集群产品边界。

                    Kubernetes Management Platform
                              Headlamp
                                  |
                     AI Diagnosis Plugin
                                  |
                                  v
                          Agent Service
                                  |
                         Diagnosis Loop
                                  |
                                  v
                      ai-agent-connector
                       /       |       \
                      /        |        \
                     v         v         v
                Kubernetes  Prometheus  Loki
                    API
                     ^
                     |
                  ServiceAccount


评测链路：

    Case Repository
          |
          v
      Eval Runner ---------> Agent Service
          |                       |
          |                       v
          |                  Diagnosis Trace
          |                       |
          +--> Ground Truth ------+
                                  |
                                  v
                          Scorer + Reporter
                                  |
                                  v
                         Versioned Benchmark


知识增强链路：

    Authorized Documents ------------> Knowledge Ingestion
                                               |
                                               v
                                      Retrieval Index
                                               |
    Verified Incident Sessions ------> Incident Case Store
                                               |
                                               v
    Agent Service <--------- Knowledge & Experience Service
         |
         +--> evidence[]              实时事实
         +--> historical_cases[]      已验证历史经验
         +--> knowledge_references[]  带版本和来源的文档知识


告警链路：

    Prometheus
        |
        v
    Alertmanager
        |
        +------> 原有告警渠道
        |
        +------> ai-agent-connector
                       |
                       v
                  Alert Snapshot
                       |
                       v
                   Agent
                       |
                       v
               Diagnosis Session
                       |
                       v
             Kubernetes Management UI


---

# 35. 当前状态与下一步验证目标

当前状态：Phase 1 已完成单集群 Pod 人工诊断闭环；Phase 2 已完成评测基线验收。其固定回归边界是：从 Headlamp Pod 详情页点击开始，经 Agent、只读 Connector 和真实 Kubernetes API，到页面展示结构化 Root Cause（或证据不足声明）、Evidence、Confidence 与 Recommendation 结束。

下一开发阶段是 Phase 3。Phase 2 已冻结的 Kubernetes-only Benchmark 继续作为 Phase 3 实时数据源增强及 Phase 4 RAG/历史检索的共同对照基线。

Phase 3 的当前核心验证目标：

1. Prometheus/Loki 分别不可用时，Kubernetes-only 诊断和已冻结 Benchmark 不回归。
2. Diagnosis Session 能跨 Agent 重启保留并可从 Headlamp 查询。
3. Deployment、Node、PVC 入口完成真实端到端验收。
4. 四组数据源消融报告能够说明 Metrics 和 Logs 对不同故障类型的净收益。

进入 Phase 4 前，必须先稳定 Phase 3 的实时事实链，并准备具有版本、权限、有效期和来源的文档集，以及经过人工确认和结果验证的历史 Incident Case。Phase 4 的首要验证不是“接入向量库”，而是知识检索能否改善调查路径，同时在知识冲突、过期、越权或不可用时坚持实时 Evidence 优先并安全降级。


---

# 36. 核心设计原则

整个系统最终遵循以下原则：

## Kubernetes 管理平台负责入口

    用户在哪里看到问题
    就在哪里点击智能诊断


## Alert 负责主动触发（Phase 5 引入）

    不需要等人发现问题


## Connector 负责事实

    Kubernetes   （Phase 1）
    Prometheus   （Phase 3 可选）
    Loki         （Phase 3 可选）


## Agent 负责推理

    Hypothesis

    Investigation

    Correlation

    Root Cause


## 不给 Agent kubeconfig

    Agent 不直接拥有 Kubernetes 权限


## 不为每个 Resource 写 Analyzer

    使用通用 Resource + Relations + Events + Metrics + Logs


## 不一次收集所有数据

    Agent 按需调查


## AI 输出必须可解释

    结论必须有 Evidence


## Phase 1 只诊断

    单集群、人工触发、只读 Kubernetes 事实，不修改生产资源


## Phase 2 建立评测与能力改进闭环

    Case + Trace + Scorer + Benchmark + Regression，不扩展生产诊断范围


## Phase 3 丰富单集群诊断与历史

    可选 Prometheus/Loki、持久化 Session、更多资源入口，并用消融评测证明收益


## Phase 4 引入知识与历史经验

    RAG 和相似 Incident 只辅助假设与调查，实时 Evidence 永远优先


## Phase 5 告警自动诊断

    复用人工诊断链路，firing 去重、resolved 关闭、再次 firing 新建


## Phase 6 生成修复计划

    只生成与审批，不直接执行


## Phase 7 才开放安全自动执行

    Gateway + Policy + Executor + 失败停止与审计


最终产品形态不是：

    Kubernetes ChatBot

而是：

    Kubernetes Management Platform
                +
        AI Diagnosis Engine
                +
        Event-driven AIOps
