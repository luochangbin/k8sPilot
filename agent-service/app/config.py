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
        self.agent_port: int = int(_env("AGENT_PORT", "8000"))
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

        self.max_tool_calls: int = int(_env("MAX_TOOL_CALLS", "12"))

        # Phase 2 eval trace: when set, per-diagnosis JSONL trace files are
        # written to this directory. Empty disables tracing (product default).
        self.trace_dir: str = _env("TRACE_DIR", "").strip()

        # Phase 3 persistence: SQLite file path; empty keeps in-memory store.
        self.db_path: str = _env("DIAGNOSIS_DB", "").strip()

        # Phase 4 knowledge & experience: SQLite file path; empty disables
        # the retrieval module (agent then runs Phase 3-only).
        self.knowledge_db_path: str = _env("KNOWLEDGE_DB", "").strip()
