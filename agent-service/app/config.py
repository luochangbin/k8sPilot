"""Runtime configuration for the agent service.

Precedence: OS environment variables > agent-service `.env` file > defaults.
The `.env` file lives at the agent-service root so the service owns its
runtime configuration.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# config.py lives at <root>/agent-service/app/config.py; parents[1] is the
# agent-service root. load_dotenv defaults to override=False, so real
# environment variables win over the service-local .env file.
_AGENT_SERVICE_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_AGENT_SERVICE_ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


class Config:
    """Phase 1 agent service configuration."""

    def __init__(self) -> None:
        self.agent_port: int = int(_env("AGENT_PORT", "8001"))
        self.connector_base_url: str = _env(
            "CONNECTOR_BASE_URL",
            "http://ai-agent-connector.k8spilot.svc.cluster.local:8080",
        )
        self.connector_timeout: float = float(_env("CONNECTOR_TIMEOUT_SECONDS", "15"))

        self.llm_base_url: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
        self.llm_api_key: str = _env("LLM_API_KEY", "")
        self.llm_model: str = _env("LLM_MODEL", "gpt-4o-mini")
        self.llm_timeout: float = float(_env("LLM_TIMEOUT_SECONDS", "120"))
        self.llm_max_retries: int = int(_env("LLM_MAX_RETRIES", "2"))
        self.llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "2048"))

        # Investigation budgets (design: rounds != tool calls).
        #   max_agent_rounds = max LLM decision responses (a rejected multi-tool
        #     response still consumes a round).
        #   max_tool_calls   = max investigation tools actually executed;
        #     submit_result never counts, policy-rejected calls never count.
        self.max_agent_rounds: int = int(_env("MAX_AGENT_ROUNDS", "12"))
        self.max_tool_calls: int = int(_env("MAX_TOOL_CALLS", "12"))
        # Global worker pool: bounds concurrent investigations so a burst of
        # requests cannot spawn unbounded LLM/connector load. Requests beyond the
        # limit stay "queued" until a worker frees up.
        self.max_concurrent_diagnoses: int = int(_env("MAX_CONCURRENT_DIAGNOSES", "4"))
        # Terminal-only finalization after the investigation budgets run out: how
        # many submit_result-only rounds the model gets (default 1). Recorded in
        # the trace so a benchmark run is reproducible.
        self.max_finalization_attempts: int = int(_env("AGENT_FINALIZATION_ATTEMPTS", "1"))

        # Phase 2 eval trace: when set, per-diagnosis JSONL trace files are
        # written to this directory. Empty disables tracing (product default).
        self.trace_dir: str = _env("TRACE_DIR", "").strip()

        # Phase 3 persistence: SQLite file path; empty keeps in-memory store.
        self.db_path: str = _env("DIAGNOSIS_DB", "").strip()

        # Phase 4 knowledge & experience: SQLite file path; empty disables
        # the retrieval module (agent then runs Phase 3-only).
        self.knowledge_db_path: str = _env("KNOWLEDGE_DB", "").strip()

        # Model profiles (handoff §4). The YAML file path is resolved relative
        # to the agent-service root when not absolute; empty keeps the legacy
        # LLM_* environment mapping as the `default` profile.
        self.llm_profiles_file: str = _resolve_agent_path(_env("LLM_PROFILES_FILE", "").strip())
        # Explicit per-request profile selection is off by default and turned on
        # by evaluation/service deployments that need it (handoff §5).
        self.enable_model_profile_selection: bool = _env(
            "ENABLE_MODEL_PROFILE_SELECTION", "false"
        ).strip().lower() in ("1", "true", "yes", "on")

        # Phase 5 alert storm protection: max alert-triggered requests accepted
        # per rolling minute (0 disables the limiter).
        self.alert_rate_limit_per_minute: int = int(_env("ALERT_RATE_LIMIT_PER_MINUTE", "30"))

        # How long a claimed-but-not-yet-started alert lifecycle may block
        # retries before another delivery may take it over (seconds).
        self.alert_claim_stale_seconds: int = int(_env("ALERT_CLAIM_STALE_SECONDS", "300"))


# agent-service package root (parent of app/); profile paths are relative to it.
_AGENT_SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _resolve_agent_path(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    return str(path if path.is_absolute() else _AGENT_SERVICE_ROOT / path)
