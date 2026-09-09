"""Seed knowledge documents and verified incident cases (Phase 4).

Authored, versioned ops content with validity/ACL metadata; incidents mirror
the Phase 2/3 eval cases that we have verified end-to-end.
"""

from .models import IncidentCase, KnowledgeDocument

ENVS = ["production", "staging"]
ACL = ["team:sre", "role:sre"]

SEED_DOCUMENTS: list[KnowledgeDocument] = [
    KnowledgeDocument(
        document_id="kb-k8s-oom-001",
        source_type="runbook",
        title="Pod OOMKilled 处理手册",
        source_uri="https://internal.example/runbooks/pod-oomkilled",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状识别
Pod 中容器反复退出，containerStatuses 显示 lastState.terminated.reason=OOMKilled，
exitCode 通常为 137（128+9，SIGKILL）。事件中出现 OOMKilling。

## 排查步骤
1. 用 query_metrics 查该 Pod 的 container_memory_working_set_bytes 峰值，与 spec 里
   resources.limits.memory 对比：峰值贴近或超过 limit 即高度指向内存超限。
2. 查看应用日志是否出现 OutOfMemoryError / heap 相关报错。
3. 检查 node 是否有内存压力事件。

## 处置
- 若应用内存模型正常，提高 memory limit（需容量评估）。
- 若为泄漏，先修应用；临时可提高 limit 止血。
- JVM 类应用检查 -Xmx 与容器 limit 的关系，避免堆外内存超限。

## 验证
- 修改后观察 restartCount 不再增长、lastState 无新 OOMKilled、metrics 峰值 < limit。
""",
    ),
    KnowledgeDocument(
        document_id="kb-k8s-imagepull-001",
        source_type="known_issue",
        title="ImagePullBackOff / ErrImagePull 已知问题清单",
        source_uri="https://internal.example/known-issues/imagepullbackoff",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状
Pod 停在 ContainerCreating/ImagePullBackOff，containerStatuses.state.waiting.reason
= ErrImagePull 或 ImagePullBackOff；事件 reason=Failed，消息含 manifest unknown / pull access denied。

## 常见原因与判定
- registry.example 私有仓库无凭据：消息含 authentication required / pull access denied。
- 镜像 tag 不存在：消息含 manifest unknown / not found。
- 镜像名拼写或 registry 域名错误：先核对 spec.containers[].image。

## 处置
- 无凭据 -> 创建 imagePullSecret 并在 pod spec 引用。
- tag 不存在 -> 修正镜像 tag 或用存在的 tag。
- 核对后可删除旧 pod 让其重建。
""",
    ),
    KnowledgeDocument(
        document_id="kb-k8s-scheduling-001",
        source_type="runbook",
        title="FailedScheduling 排查手册",
        source_uri="https://internal.example/runbooks/failedscheduling",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状
Pod 长期 Pending，condition PodScheduled=False，事件 reason=FailedScheduling。

## 判定
- 事件消息 0/1 nodes are available: 后跟 1 Insufficient cpu/memory -> 资源不足。
- 0/3 nodes available 无 nodeSelector 匹配 -> 标签/亲和性不匹配。
- 关注 message 中的具体谓词：Insufficient cpu、Insufficient memory、node(s) didn't match nodeSelector。

## 处置
- 资源不足：降低 request 或扩容/腾节点。
- 标签不匹配：核对 nodeSelector/affinity 与节点 labels。
- node NotReady：先修节点。
""",
    ),
    KnowledgeDocument(
        document_id="kb-k8s-crashloop-001",
        source_type="runbook",
        title="CrashLoopBackOff 排查手册",
        source_uri="https://internal.example/runbooks/crashloopbackoff",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状
容器反复启动后立即退出，state.waiting.reason=CrashLoopBackOff，restartCount 持续增长。

## 排查顺序
1. 先看 events 与 lastState：reason 是 OOMKilled(内存) 还是 Error(业务崩溃)。
2. logs previous=true 取上一容器输出：业务异常堆栈/致命日志是根因关键。
3. 区分：ExitCode 137/SIGKILL 多与 OOM/被杀有关；ExitCode 1 多为业务启动失败。

## 处置
- 业务崩溃：修应用并核对启动参数/环境变量/依赖(数据库连接等)。
- 配置错误导致启动失败：核对 ConfigMap/Secret 挂载与 env。
""",
    ),
    KnowledgeDocument(
        document_id="kb-k8s-config-001",
        source_type="runbook",
        title="容器配置错误（CreateContainerConfigError）处理",
        source_uri="https://internal.example/runbooks/configerror",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状
容器无法启动，state.waiting.reason=CreateContainerConfigError，事件 FailedMount/
Failed 消息指明 configmap/secret 缺失或 key 不存在。

## 判定
- 消息 contains configmap "x" not found -> 引用了不存在的 ConfigMap。
- envFrom/volumeMount 引用错误是常见根因；事件比容器日志更有信息量。

## 处置
- 创建缺失的 ConfigMap/Secret，或修正 spec 引用。
""",
    ),
    KnowledgeDocument(
        document_id="kb-k8s-pvc-001",
        source_type="known_issue",
        title="PVC Pending / 卷挂载失败处理",
        source_uri="https://internal.example/known-issues/pvc-pending",
        product="kubernetes",
        versions=["1.x"],
        environments=ENVS,
        owner="k8s-sre",
        valid_from="2024-01-01T00:00:00+00:00",
        acl_tags=ACL,
        content="""
## 症状
Pod Pending/ContainerCreating，事件 FailedMount；PVC phase=Pending。

## 判定
- PVC phase=Pending 且 storageClassName 不存在 -> 无 provisioner 可用。
- 事件 message 含 FailedMount / 卷未绑定 -> 等 PVC Bound 后 Pod 才能挂载。

## 处置
- 为 PVC 指定已存在的 StorageClass 或安装对应 provisioner。
- 修复 PVC 后 Pod 自动恢复。
""",
    ),
]

SEED_INCIDENTS: list[IncidentCase] = [
    IncidentCase(
        incident_id="inc-2026-oom-001",
        status="verified",
        product="payment-api",
        product_version="2.4.1",
        environment="production",
        resource_kind="Pod",
        symptoms=["CrashLoopBackOff", "OOMKilled"],
        root_cause_code="CONTAINER_OOMKILLED",
        remediation_summary="容量评估后提高 memory limit 至 256Mi，JVM 加 -Xmx 约束。",
        verification={"outcome": "success", "verified_at": "2026-08-30T00:00:00Z"},
        evidence_summary="lastState.terminated.reason=OOMKilled, exitCode=137, memory 峰值贴近 limit。",
    ),
    IncidentCase(
        incident_id="inc-2026-imagepull-001",
        status="verified",
        product="payment-api",
        product_version="2.4.1",
        environment="production",
        resource_kind="Pod",
        symptoms=["ImagePullBackOff"],
        root_cause_code="IMAGE_PULL_FAILED",
        remediation_summary="修正镜像 tag 为已存在版本后重建。",
        verification={"outcome": "success", "verified_at": "2026-08-30T00:00:00Z"},
        evidence_summary="state.waiting.reason=ImagePullBackOff, 事件 manifest unknown。",
    ),
    IncidentCase(
        incident_id="inc-2026-sched-001",
        status="verified",
        product="batch-worker",
        product_version="1.9.0",
        environment="production",
        resource_kind="Pod",
        symptoms=["Pending", "FailedScheduling"],
        root_cause_code="SCHEDULING_FAILED",
        remediation_summary="核对 nodeSelector 标签后 Pod 成功调度。",
        verification={"outcome": "success", "verified_at": "2026-08-31T00:00:00Z"},
        evidence_summary="事件 FailedScheduling: 0/3 nodes available，无节点匹配 nodeSelector。",
    ),
    IncidentCase(
        incident_id="inc-2026-crashloop-001",
        status="verified",
        product="payment-api",
        product_version="2.4.1",
        environment="staging",
        resource_kind="Pod",
        symptoms=["CrashLoopBackOff"],
        root_cause_code="CRASH_LOOP_BACKOFF",
        remediation_summary="应用启动连接数据库超时；修复连接池配置后重启。",
        verification={"outcome": "success", "verified_at": "2026-09-01T00:00:00Z"},
        evidence_summary="容器 exitCode=1，上一容器日志显示数据库连接失败。",
    ),
    IncidentCase(
        incident_id="inc-2026-config-001",
        status="verified",
        product="payment-api",
        product_version="2.4.1",
        environment="production",
        resource_kind="Pod",
        symptoms=["CreateContainerConfigError"],
        root_cause_code="CONFIG_ERROR",
        remediation_summary="创建缺失的 ConfigMap 后容器正常启动。",
        verification={"outcome": "success", "verified_at": "2026-09-01T00:00:00Z"},
        evidence_summary="事件: configmap 'payment-config' not found。",
    ),
]
