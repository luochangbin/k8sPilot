"""Model profile resolution tests (handoff §4)."""

import json

import pytest

from app.llm import ProfileError, UnknownProfileError, resolve_model


class FakeCfg:
    llm_profiles_file = ""
    llm_base_url = "https://api.deepseek.com"
    llm_api_key = "env-key"
    llm_model = "deepseek-v4-flash"
    llm_timeout = 120.0
    llm_max_retries = 2
    llm_max_tokens = 2048


def _cfg_with_file(path):
    cfg = FakeCfg()
    cfg.llm_profiles_file = str(path)
    return cfg


VALID = """
default_profile: default
providers:
  commandcode:
    protocol: openai_chat_completions
    base_url: https://api.commandcode.ai/provider/v1
    api_key_env: CMD_KEY
    headers: {}
models:
  default:
    provider: commandcode
    remote_model_id: deepseek/deepseek-v4-flash
    parameters:
      max_tokens: 2048
      temperature: 0.2
    timeout_seconds: 60
    max_retries: 1
"""


def test_default_profile_maps_env_configuration():
    resolved = resolve_model(FakeCfg(), None)
    assert resolved.provider == "env"
    assert resolved.remote_model_id == "deepseek-v4-flash"
    assert "env-key" not in json.dumps(resolved.public_metadata())


def test_explicit_profile_without_file_is_unknown():
    with pytest.raises(UnknownProfileError):
        resolve_model(FakeCfg(), "anything")


def test_valid_file_resolves_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("CMD_KEY", "secret-value")
    path = tmp_path / "profiles.yaml"
    path.write_text(VALID, encoding="utf-8")
    resolved = resolve_model(_cfg_with_file(path), None)
    assert resolved.profile == "default"
    assert resolved.provider == "commandcode"
    assert resolved.remote_model_id == "deepseek/deepseek-v4-flash"
    assert resolved.parameters == {"max_tokens": 2048, "temperature": 0.2}
    assert resolved.timeout_seconds == 60
    assert resolved.max_retries == 1
    assert "secret-value" not in json.dumps(resolved.public_metadata())
    assert "secret-value" not in resolved.config_fingerprint()


def test_unknown_profile_in_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CMD_KEY", "x")
    path = tmp_path / "profiles.yaml"
    path.write_text(VALID, encoding="utf-8")
    with pytest.raises(UnknownProfileError):
        resolve_model(_cfg_with_file(path), "nope")


def test_missing_key_env_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CMD_KEY", raising=False)
    path = tmp_path / "profiles.yaml"
    path.write_text(VALID, encoding="utf-8")
    with pytest.raises(ProfileError, match="CMD_KEY"):
        resolve_model(_cfg_with_file(path), None)


def test_duplicate_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CMD_KEY", "x")
    path = tmp_path / "profiles.yaml"
    path.write_text(
        "default_profile: a\ndefault_profile: b\nproviders: {}\nmodels: {}\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="duplicate"):
        resolve_model(_cfg_with_file(path), None)


def test_unsupported_parameter_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CMD_KEY", "x")
    bad = VALID.replace("temperature: 0.2", "reasoning_effort: high")
    path = tmp_path / "profiles.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ProfileError, match="unsupported parameters"):
        resolve_model(_cfg_with_file(path), None)


def test_unsupported_protocol_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CMD_KEY", "x")
    bad = VALID.replace("openai_chat_completions", "anthropic_messages")
    path = tmp_path / "profiles.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ProfileError, match="unsupported protocol"):
        resolve_model(_cfg_with_file(path), None)

class _FakeCompletions:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        return {"ok": True}


def _fake_llm(max_retries, fail_times):
    from types import SimpleNamespace
    from app.llm import OpenAILLM
    obj = OpenAILLM.__new__(OpenAILLM)
    obj._init(base_url="http://x", api_key="k", model="m", timeout=5,
              max_retries=max_retries, max_tokens=10, temperature=None,
              default_headers=None)
    obj._client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(fail_times)))
    return obj


def test_chat_reports_actual_request_attempts_on_success():
    llm = _fake_llm(max_retries=3, fail_times=1)
    resp = llm.chat([], [], "auto")
    assert resp == {"ok": True}
    assert llm.last_attempt_count == 2
    assert llm.max_retries == 3


def test_chat_failure_carries_attempts():
    from app.llm import LLMError
    llm = _fake_llm(max_retries=1, fail_times=5)
    try:
        llm.chat([], [], "auto")
        raise AssertionError("expected LLMError")
    except LLMError as exc:
        assert exc.attempts == 2
    assert llm.last_attempt_count == 2
