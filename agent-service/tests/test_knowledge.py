"""Phase 4 knowledge/incident module tests: ingest, filter, search, agent gating."""

import json

import pytest

from app.agent import Agent
from app.config import Config
from app.knowledge.ingest import chunk_document
from app.knowledge.models import IncidentCase, KnowledgeDocument
from app.knowledge.service import KnowledgeService
from app.knowledge.store import KnowledgeStore
from app.models import DiagnosisRequest, ResourceRef, Trigger
from app.store import SessionStore

from .fakes import ScriptedLLM, StubConnector


def make_knowledge():
    store = KnowledgeStore()
    doc = KnowledgeDocument(
        document_id="kb-oom", source_type="runbook", title="OOM runbook",
        source_uri="http://x", product="kubernetes", versions=["1.x"],
        valid_from="2024-01-01T00:00:00+00:00", status="active",
        content="## 排查\nOOMKilled means the container exceeded its memory limit.\n## 处置\nRaise memory limit after capacity review.")
    store.upsert_document(doc, chunk_document(doc))
    store.upsert_incident(IncidentCase(
        incident_id="inc-1", status="verified", product="payment-api", product_version="2.4.1",
        resource_kind="Pod", symptoms=["CrashLoopBackOff", "OOMKilled"],
        root_cause_code="CONTAINER_OOMKILLED", remediation_summary="increase memory limit",
        evidence_summary="lastState OOMKilled"))
    return KnowledgeService(store)


def test_chunk_document_splits_sections():
    doc = KnowledgeDocument(document_id="d", source_type="runbook", title="t",
                            content="## A\nbody a\n\n## B\nbody b")
    chunks = chunk_document(doc)
    assert [c.section for c in chunks] == ["A", "B"]


def test_search_knowledge_returns_citations_and_filters():
    k = make_knowledge()
    refs = k.search_knowledge("OOMKilled memory limit")
    assert len(refs) >= 1
    assert refs[0].citation["document_id"] == "kb-oom"
    assert refs[0].content and refs[0].retrieval_id.startswith("kb_")
    # version filter excludes (doc is 1.x)
    assert k.search_knowledge("OOMKilled", filters={"product": "kubernetes", "versions": ["9.9"]}) == []


def test_search_incidents_only_verified():
    k = make_knowledge()
    k._store.upsert_incident(IncidentCase(incident_id="inc-unverified", status="open",
                                          product="x", root_cause_code="HEALTHY",
                                          symptoms=["Running"]))
    hits = k.search_incidents("OOMKilled CrashLoop")
    assert len(hits) == 1
    assert hits[0].incident_id == "inc-1"
    assert hits[0].root_cause_code == "CONTAINER_OOMKILLED"


def _agent_with_knowledge(knowledge):
    cfg = Config()
    cfg.max_tool_calls = 12
    return Agent(cfg, StubConnector(), ScriptedLLM([]), knowledge=knowledge)


def test_retrieval_tools_gated_by_request_and_references_resolved():
    knowledge = make_knowledge()
    seen_tools = []

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            seen_tools.append([t["function"]["name"] for t in tools])
            if self.script:
                return self.script.pop(0)
            rid = None
            for m in reversed(messages):
                if m.get("role") == "tool":
                    try:
                        res = json.loads(m["content"])
                        if res:
                            rid = res[0]["retrieval_id"]
                            break
                    except (ValueError, KeyError):
                        pass
            assert rid, "expected a search_knowledge result with a retrieval_id"
            return ScriptedLLM.tool_response("submit_result", {
                "symptom": "容器重启", "root_cause_code": "CONTAINER_OOMKILLED",
                "insufficient_evidence": False,
                "evidence": [{"source": "kubernetes.status",
                              "path": "actual_state.restart_count", "operator": "equals",
                              "value": "37", "summary": "OOMKilled"}],
                "root_cause": "内存超限", "confidence": "high", "recommendations": [],
                "knowledge_references": [{"retrieval_id": rid, "used_for": "explanation"}],
            })

    llm = RecordingLLM([
        ScriptedLLM.tool_response("search_knowledge", {"query": "OOMKilled memory limit"}),
        # A real-time tool result is required before an explicit conclusion can
        # pass the deterministic gate; retrieval hits never count as evidence.
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "n", "name": "p"}),
    ])
    req = DiagnosisRequest(trigger=Trigger.manual,
                           resource=ResourceRef(kind="Pod", namespace="n", name="p", uid="u"),
                           enable_knowledge=True, enable_incidents=False)
    store = SessionStore()
    d = store.create(req)
    agent = Agent(Config(), StubConnector(), llm, knowledge=knowledge)
    agent.run(req, store, d.diagnosis_id)

    assert "search_knowledge" in seen_tools[0]
    assert "search_incidents" not in seen_tools[0]
    got = store.get(d.diagnosis_id)
    assert got.status == "completed"
    # The cited retrieval_id must resolve to the actual returned reference.
    assert got.result.knowledge_references
    ref = got.result.knowledge_references[0]
    assert ref["citation"]["document_id"] == "kb-oom"
    assert ref["used_for"] == "explanation"


def test_retrieval_off_when_no_knowledge_module():
    cfg = Config()
    req = DiagnosisRequest(trigger=Trigger.manual,
                           resource=ResourceRef(kind="Pod", namespace="n", name="p", uid="u"))
    agent = Agent(cfg, StubConnector(), ScriptedLLM([]), knowledge=None)
    assert agent._retrieval_flags(req) == {"knowledge": False, "incidents": False}


def test_incident_search_used_by_agent():
    knowledge = make_knowledge()
    seen_tools = []

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            seen_tools.append([t["function"]["name"] for t in tools])
            if self.script:
                return self.script.pop(0)
            rid = None
            for m in reversed(messages):
                if m.get("role") == "tool":
                    try:
                        res = json.loads(m["content"])
                        if res:
                            rid = res[0]["retrieval_id"]
                            break
                    except (ValueError, KeyError):
                        pass
            assert rid, "expected a search_incidents result"
            return ScriptedLLM.tool_response("submit_result", {
                "symptom": "s", "root_cause_code": "", "insufficient_evidence": True,
                "evidence": [], "root_cause": "", "confidence": "low", "recommendations": [],
                "historical_cases": [{"retrieval_id": rid, "used_for": "hypothesis"}],
            })

    llm = RecordingLLM([
        ScriptedLLM.tool_response("search_incidents", {
            "symptoms": ["CrashLoopBackOff", "OOMKilled"],
            "root_cause_candidates": ["CONTAINER_OOMKILLED"]}),
    ])
    req = DiagnosisRequest(trigger=Trigger.manual,
                           resource=ResourceRef(kind="Pod", namespace="n", name="p", uid="u"),
                           enable_incidents=True)
    store = SessionStore()
    d = store.create(req)
    Agent(Config(), StubConnector(), llm, knowledge=knowledge).run(req, store, d.diagnosis_id)
    got = store.get(d.diagnosis_id)
    assert got.status == "completed"
    assert got.result.historical_cases
    assert got.result.historical_cases[0]["incident_id"] == "inc-1"
