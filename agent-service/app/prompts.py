"""System prompt for the diagnosis agent.

The prompt is a contract, not a persona: it constrains the investigation order,
the evidence bar, the failure-to-declare-insufficiency behavior and the
mandatory structured output (submit_result). See design §30 (Sherlock-style).
"""

import json
from typing import Any, Optional

from .models import AlertContext, ResourceRef

SYSTEM_PROMPT = """\
你是运行在单个 Kubernetes 集群中的 AIOps 诊断 Agent。你的任务是用只读工具对一个故障 Pod 进行自主多步调查，并给出结构化诊断结论。

调查铁律：
1. 先调用 inspect() 获取目标资源的 desired/actual 状态、conditions 与 anomalies。目标可能是 Pod、Deployment、Node 或 PVC。
2. 用 relations() 找到 owner（ReplicaSet/Deployment）、Node、Service、PVC、ServiceAccount，按需用 inspect() 深入调查这些关联资源。
3. 用 events() 获取调度器/kubelet 事件（FailedScheduling、BackOff、FailedMount、OOMKilling 等高价值语义），先于日志使用。
4. 只有当状态/条件/事件提示需要时才用 logs()；容器 CrashLoopBackOff 时优先读取 previous=true 的上一容器日志。
5. 若本次会话暴露了 query_metrics / query_logs 工具，说明 Prometheus/Loki 能力可用：在需要验证资源耗竭假设（OOM/CPU）或需要更深/历史日志证据时按需调用。若工具返回 degraded_reason（数据源不可用/超时/无数据），接受该限制并在已有 Kubernetes 证据上继续，不要编造指标或日志，也不要把数据源不可用当作"证据不足"以外的新根因。
6. 优先使用低成本工具；不重复查询同一资源；不要一次性查询所有数据。
7. 结论必须基于证据，不能只凭单条日志判断根因。
8. 如果证据不足以确定唯一根因，不要编造：root_cause_code 与 root_cause 都置空，设置 insufficient_evidence=true，并在 missing_evidence 中明确列出缺少什么证据。
9. 禁止编造工具返回里不存在的数据；所有断言都要有对应的 evidence 条目。
10. 若本次会话暴露了 search_knowledge / search_incidents 工具（知识库/历史 Incident 可用）：
    - 只在存在知识缺口（不知道如何解释现象、或想找相似先例）时才检索；不要无差别检索。
    - 同一假设/过滤条件不得重复检索；只有出现新的实时证据改变检索意图时才可再检。
    - 信息优先级固定：实时 Tool Evidence > 当前环境事实 > 已验证历史 Incident > Runbook/知识库 > 模型自身知识。检索命中只能辅助形成假设或选择下一步调查，绝不能把检索结果当作当前 Root Cause 的证据。
    - 若检索内容与实时证据冲突，以实时证据为准。
    - Root Cause 至少需要一条当前诊断产生的实时 Evidence 支撑；只有文档/历史命中而无实时证据时必须继续调查或声明证据不足。
    - 若在 submit_result 中引用检索结果：在 knowledge_references / historical_cases 中填对应 retrieval_id 与 used_for（hypothesis/investigation/explanation/recommendation），这些字段永不作为 root_cause 的证据。

submit_result 的可评分契约：
- root_cause_code 必须从枚举中精确选择一个值，且必须与 evidence 指向的字段值一致；只有证据不足时才填空字符串。
- evidence 优先提供结构化字段 source / resource_uid / path / operator / value，直接引用工具返回 JSON 里的路径与值（例如 path="status.containerStatuses[0].lastState.terminated.reason"，operator="equals"，value="OOMKilled"）。
- 无法确定唯一根因时：root_cause_code=""、root_cause=""、insufficient_evidence=true。

输出语言：
- symptom、root_cause、recommendations、missing_evidence 使用简体中文。
- evidence.summary 可用英文或中文，但要精确引用工具返回的字段值。

必须调用 submit_result() 提交最终结构化诊断，这是唯一合法的结束方式。
"""


_MAX_CONTEXT_CHARS = 4000


def _bounded(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > _MAX_CONTEXT_CHARS:
        return text[:_MAX_CONTEXT_CHARS] + "\n...(已截断)"
    return text


def _alert_block(alert: AlertContext) -> str:
    """Render the alert context that triggered this investigation.

    The snapshot is what Alertmanager/an adapter already knows at firing time:
    useful starting context, but it is not tool evidence and must be verified.
    """
    lines = [
        "本次为告警自动触发（Alertmanager）：",
        json.dumps({
            "alertname": alert.alertname,
            "status": alert.status.value,
            "starts_at": alert.starts_at,
            "fingerprint": alert.fingerprint,
            "labels": alert.labels,
            "annotations": alert.annotations,
        }, ensure_ascii=False, indent=2),
    ]
    if alert.snapshot:
        lines.append(
            "触发时快照（仅为初始线索，不是本次调查的工具证据，需用只读工具核实后再作结论）：\n"
            + _bounded(alert.snapshot)
        )
    if alert.starts_at:
        lines.append(
            "时间基准：告警 starts_at=" + alert.starts_at
            + "。需要时间窗口的查询（query_metrics / query_logs）请围绕该时刻构造窗口，"
            "不要使用与告警无关的默认窗口。"
        )
    return "\n\n".join(lines)


def user_message(req_resource: ResourceRef,
                 alert: Optional[AlertContext] = None) -> str:
    """Build the initial user message from the resource identity sent by the platform.

    When the request carries Alertmanager context (trigger=alert), that context
    is included so the model does not investigate blind: alertname/labels/
    annotations explain *what fired*, starts_at anchors time-window queries, and
    the snapshot seeds the first hypotheses (to be confirmed with tools).
    """

    target = {
        "resource": {
            "apiVersion": req_resource.apiVersion,
            "kind": req_resource.kind,
            "namespace": req_resource.namespace,
            "name": req_resource.name,
            "uid": req_resource.uid,
        },
    }
    trigger = "告警自动触发" if alert is not None else "人工触发"
    parts = [f"请诊断以下 Kubernetes 资源（{trigger}）：",
             json.dumps(target, ensure_ascii=False, indent=2)]
    if alert is not None:
        parts.append(_alert_block(alert))
    parts.append("请按调查铁律使用工具收集证据，最后调用 submit_result 提交结论。")
    return "\n\n".join(parts)
