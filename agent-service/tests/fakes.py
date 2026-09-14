"""Shared fakes for agent tests."""

import json
from types import SimpleNamespace
from typing import Any, Optional

from app.connector import ConnectorError, ToolError


class StubConnector:
    """Scripted connector double: raises or returns canned responses."""

    def __init__(self, *, raise_on: Optional[str] = None,
                 inspect_response: Optional[dict] = None,
                 capabilities: Optional[dict] = None,
                 call_log: Optional[list] = None) -> None:
        self._raise_on = raise_on
        self._inspect_response = inspect_response
        self._capabilities = capabilities
        self.call_log = call_log if call_log is not None else []

    def capabilities(self) -> dict[str, Any]:
        return self._capabilities if self._capabilities is not None else {}

    def _maybe_raise(self, name: str) -> None:
        self.call_log.append(name)
        if self._raise_on == "connector":
            raise ConnectorError("connector unreachable at http://test: test")
        if self._raise_on == "not_found" and name == "inspect":
            raise ToolError("not_found", "pods \"x\" not found")

    def inspect(self, target: dict[str, Any]) -> dict[str, Any]:
        self._maybe_raise("inspect")
        return self._inspect_response or {
            "target": target,
            "exists": True,
            "uid_mismatch": False,
            "desired_state": {"restart_policy": "Always"},
            "actual_state": {"phase": "Running", "restart_count": 37},
            "conditions": [],
            "anomalies": ["container payment-api last terminated reason=OOMKilled"],
        }

    def relations(self, target: dict[str, Any]) -> dict[str, Any]:
        self._maybe_raise("relations")
        return {"target": target, "relations": []}

    def events(self, target: dict[str, Any], **kwargs) -> dict[str, Any]:
        self._maybe_raise("events")
        return {"target": target, "count": 0, "events": [], "truncated": False}

    def logs(self, target: dict[str, Any], **kwargs) -> dict[str, Any]:
        self._maybe_raise("logs")
        return {"namespace": target.get("namespace"), "pod": target.get("name"),
                "data": "line1\nline2", "truncated": False}


class ScriptedLLM:
    """Scripted chat-completions double."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[Any] = []
        self.forced_choices: list[Any] = []

    def chat(self, messages: list[dict], tools: list[dict], tool_choice: Any):
        self.calls.append(tool_choice)
        if not self.script:
            raise AssertionError("script exhausted")
        return self.script.pop(0)

    @staticmethod
    def tool_response(name: str, args: dict, call_id: str = "call_1") -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id=call_id,
                        type="function",
                        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
                    )],
                ),
            )],
        )

    @staticmethod
    def tool_response_with_extra(name: str, args: dict, extra_content: dict,
                                 call_id: str = "call_1") -> Any:
        """Simulate a provider (e.g. Gemini thinking) that attaches extra data
        such as thought_signature to the function call part."""
        tc = SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        tc.model_extra = {"extra_content": extra_content}
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=None, tool_calls=[tc]),
        )])

    @staticmethod
    def tool_response_with_reasoning(name: str, args: dict, reasoning: str, call_id: str = "call_1") -> Any:
        msg = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id=call_id,
                type="function",
                function=SimpleNamespace(name=name, arguments=json.dumps(args)),
            )],
        )
        # Simulate a DeepSeek reasoning model: reasoning_content exposed as an
        # attribute on the parsed message.
        msg.reasoning_content = reasoning
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    @staticmethod
    def text_response(content: str) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))]
        )
