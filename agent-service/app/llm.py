"""Model profile resolution and the OpenAI-compatible LLM client (handoff §4).

Only server-side pre-configured profiles are allowed: requests may select a
profile name, never a URL, key or header. Profiles are declared in an optional
YAML file (``LLM_PROFILES_FILE``, resolved relative to the agent-service root).
Without that file the existing ``LLM_*`` environment configuration is mapped to
the ``default`` profile, preserving prior behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

PROTOCOL_OPENAI = "openai_chat_completions"
SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI,)
# Explicitly allowed request parameters. Anything else is rejected rather than
# silently ignored (handoff §4).
ALLOWED_PARAMETERS = ("max_tokens", "temperature")


class ProfileError(Exception):
    """Invalid or unusable model profile configuration."""


class UnknownProfileError(ProfileError):
    """Requested profile does not exist."""


class ProfileSelectionDisabled(Exception):
    """Explicit profile selection arrived while the feature is disabled."""


class _DuplicateKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of last-wins."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ProfileError(f"duplicate key in profile config: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True)
class ResolvedModel:
    """Immutable execution configuration fixed when a diagnosis is created."""

    profile: str
    provider: str
    protocol: str
    remote_model_id: str
    base_url: str
    api_key: str
    parameters: dict[str, Any]
    timeout_seconds: float
    max_retries: int
    headers: dict[str, str]

    def public_metadata(self) -> dict[str, Any]:
        """Reproducibility metadata. Never includes secrets or header values."""
        return {
            "resolved_profile": self.profile,
            "provider": self.provider,
            "protocol": self.protocol,
            "requested_model_id": self.remote_model_id,
            "effective_parameters": dict(self.parameters),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }

    def config_fingerprint(self) -> str:
        payload = {
            **self.public_metadata(),
            "base_url": self.base_url,
            "header_names": sorted(self.headers),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ExecutionContext:
    """Per-diagnosis immutable execution context (llm + model metadata)."""

    llm: Any
    metadata: dict[str, Any]


def load_profiles(profiles_path: Path) -> dict[str, Any]:
    if not profiles_path.is_file():
        raise ProfileError(f"LLM_PROFILES_FILE not found: {profiles_path}")
    try:
        raw = yaml.load(profiles_path.read_text(encoding="utf-8"),
                        Loader=_DuplicateKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML in {profiles_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"profile config {profiles_path} must be a mapping")
    return raw


def _validate_parameters(profile: str, parameters: Any) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise ProfileError(f"profile {profile!r}: parameters must be a mapping")
    unknown = set(parameters) - set(ALLOWED_PARAMETERS)
    if unknown:
        raise ProfileError(f"profile {profile!r}: unsupported parameters {sorted(unknown)}")
    return dict(parameters)


def _resolve_from_file(raw: dict[str, Any], name: Optional[str]) -> ResolvedModel:
    providers = raw.get("providers") or {}
    models = raw.get("models") or {}
    default_profile = raw.get("default_profile")
    if not isinstance(providers, dict) or not isinstance(models, dict):
        raise ProfileError("profile config requires 'providers' and 'models' mappings")
    profile = name or default_profile
    if not profile:
        raise ProfileError("profile config has no default_profile and no profile was requested")
    if profile not in models:
        raise UnknownProfileError(f"unknown model profile: {profile!r}")
    model_cfg = models[profile]
    if not isinstance(model_cfg, dict):
        raise ProfileError(f"profile {profile!r}: model entry must be a mapping")
    provider_name = model_cfg.get("provider")
    if provider_name not in providers:
        raise ProfileError(f"profile {profile!r}: unknown provider {provider_name!r}")
    provider = providers[provider_name]
    if not isinstance(provider, dict):
        raise ProfileError(f"provider {provider_name!r} must be a mapping")
    protocol = provider.get("protocol", PROTOCOL_OPENAI)
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ProfileError(f"provider {provider_name!r}: unsupported protocol {protocol!r}")
    base_url = provider.get("base_url")
    if not base_url:
        raise ProfileError(f"provider {provider_name!r}: base_url is required")
    api_key_env = provider.get("api_key_env")
    if not api_key_env:
        raise ProfileError(f"provider {provider_name!r}: api_key_env is required")
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        raise ProfileError(f"provider {provider_name!r}: environment {api_key_env} is not set")
    headers = provider.get("headers") or {}
    if not isinstance(headers, dict):
        raise ProfileError(f"provider {provider_name!r}: headers must be a mapping")
    remote_model_id = model_cfg.get("remote_model_id")
    if not remote_model_id:
        raise ProfileError(f"profile {profile!r}: remote_model_id is required")
    return ResolvedModel(
        profile=profile,
        provider=provider_name,
        protocol=protocol,
        remote_model_id=remote_model_id,
        base_url=base_url,
        api_key=api_key,
        parameters=_validate_parameters(profile, model_cfg.get("parameters")),
        timeout_seconds=float(model_cfg.get("timeout_seconds", 120)),
        max_retries=int(model_cfg.get("max_retries", 2)),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def _default_from_config(cfg: Any) -> ResolvedModel:
    return ResolvedModel(
        profile="default",
        provider="env",
        protocol=PROTOCOL_OPENAI,
        remote_model_id=cfg.llm_model,
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key or "sk-not-needed",
        parameters={"max_tokens": cfg.llm_max_tokens},
        timeout_seconds=cfg.llm_timeout,
        max_retries=cfg.llm_max_retries,
        headers={},
    )


def resolve_model(cfg: Any, requested: Optional[str] = None) -> ResolvedModel:
    """Resolve a profile name to an immutable model configuration."""
    if not getattr(cfg, "llm_profiles_file", ""):
        if requested and requested != "default":
            raise UnknownProfileError(f"unknown model profile: {requested!r}")
        return _default_from_config(cfg)
    raw = load_profiles(Path(cfg.llm_profiles_file))
    return _resolve_from_file(raw, requested)


class OpenAILLM:
    """OpenAI-compatible chat client with a single bounded retry layer."""

    def __init__(self, cfg: Any) -> None:  # backward-compatible constructor
        self._init(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key or "sk-not-needed",
            model=cfg.llm_model,
            timeout=cfg.llm_timeout,
            max_retries=cfg.llm_max_retries,
            max_tokens=cfg.llm_max_tokens,
            temperature=None,
            default_headers=None,
        )

    @classmethod
    def from_resolved(cls, resolved: ResolvedModel) -> "OpenAILLM":
        obj = cls.__new__(cls)
        obj._init(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.remote_model_id,
            timeout=resolved.timeout_seconds,
            max_retries=resolved.max_retries,
            max_tokens=int(resolved.parameters.get("max_tokens", 2048)),
            temperature=resolved.parameters.get("temperature"),
            default_headers=resolved.headers or None,
        )
        return obj

    def _init(self, *, base_url: str, api_key: str, model: str, timeout: float,
              max_retries: int, max_tokens: int, temperature: Optional[float],
              default_headers: Optional[dict[str, str]]) -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key or "sk-not-needed",
                              default_headers=default_headers)
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._last_attempt_count = 1

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def max_retries(self) -> int:
        """Effective retry budget of this client (profile-specific)."""
        return self._max_retries

    @property
    def last_attempt_count(self) -> int:
        """Actual HTTP request attempts used by the most recent chat() call."""
        return self._last_attempt_count

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any):
        last_exc: Optional[Exception] = None
        attempts = 0
        for attempt in range(self._max_retries + 1):
            attempts = attempt + 1
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "timeout": self._timeout,
                    "max_tokens": self._max_tokens,
                }
                if self._temperature is not None:
                    kwargs["temperature"] = self._temperature
                response = self._client.chat.completions.create(**kwargs)
                self._last_attempt_count = attempts
                return response
            except Exception as exc:  # noqa: BLE001 - surface any provider failure
                last_exc = exc
                if attempt == self._max_retries:
                    break
        self._last_attempt_count = attempts
        raise LLMError(f"LLM request failed after {attempts} attempts: {last_exc}",
                       attempts=attempts)


class LLMError(Exception):
    """Raised when the LLM endpoint fails after retries."""

    def __init__(self, message: str, *, attempts: Optional[int] = None) -> None:
        super().__init__(message)
        # Actual request attempts used by the failed call (None if unknown).
        self.attempts = attempts
