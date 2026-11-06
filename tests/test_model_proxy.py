"""Exercise real-mode guards with provider HTTP replaced entirely in process."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from runtime import proxy


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "real")
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("MODEL_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "approved-model")
    observed = []
    reply = {"model": "approved-model", "usage": {"prompt_tokens": 10, "completion_tokens": 3},
             "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Done"}}]}

    def post(*args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: reply)

    monkeypatch.setattr(proxy.httpx, "post", post)
    return reply, observed


def request(**changes):
    return {"messages": [{"role": "user", "content": "Read the file"}], "tools": [],
            "expected_mode": "real", "expected_model": "approved-model", **changes}


@pytest.mark.parametrize("changes", [
    {"expected_mode": "mock"}, {"expected_mode": "unknown"},
    {"expected_model": None}, {"expected_model": ""}, {"expected_model": "another-model"},
])
def test_expectation_mismatch_never_calls_provider(provider, changes):
    with pytest.raises(HTTPException):
        proxy.completion(request(**changes))
    assert provider[1] == []


def test_omitted_expectation_defaults_to_mock_and_blocks_real(provider):
    body = request()
    del body["expected_mode"]
    with pytest.raises(HTTPException) as error:
        proxy.completion(body)
    assert error.value.status_code == 409
    assert provider[1] == []


def test_real_expectation_cannot_run_against_replay(monkeypatch, provider):
    monkeypatch.setenv("MODEL_MODE", "mock")
    with pytest.raises(HTTPException):
        proxy.completion(request())
    assert provider[1] == []


def test_health_discloses_only_mode_and_model(monkeypatch, provider):
    assert proxy.health() == {"status": "ready", "mode": "real", "model": "approved-model"}
    monkeypatch.setenv("MODEL_MODE", "mock")
    assert proxy.health() == {"status": "ready", "mode": "mock", "model": None}
    proxy.completion(request(expected_mode="mock", expected_model=None))
    assert provider[1] == []


def test_complete_response_retains_usage_and_disables_environment_proxy(provider):
    result = proxy.completion(request())
    assert result["message"]["content"] == "Done"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert "error" not in result
    assert provider[1][0][1]["trust_env"] is False
    assert provider[1][0][1]["json"]["model"] == "approved-model"


def test_tool_completion_is_allowed(provider):
    provider[0]["choices"][0] = {"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "ReadFile", "arguments": "{}"}}]}}
    result = proxy.completion(request())
    assert "error" not in result
    assert result["message"]["tool_calls"]


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "unknown", None])
def test_incomplete_response_preserves_usage_without_returning_actions(provider, finish_reason):
    provider[0]["choices"][0]["finish_reason"] = finish_reason
    provider[0]["choices"][0]["message"]["tool_calls"] = [{"id": "must-not-execute"}]
    result = proxy.completion(request())
    assert result["error"] == "incomplete_response"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert not result["message"].get("tool_calls")
    assert len(provider[1]) == 1


@pytest.mark.parametrize("choice", [
    {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "No tools"}},
    {"finish_reason": "stop", "message": {"role": "assistant", "tool_calls": [{"id": "x"}]}},
    {"finish_reason": "stop", "message": {"role": "user", "content": "Done"}},
    {"finish_reason": "stop", "message": {"role": "assistant", "content": None}},
    {},
])
def test_inconsistent_completion_is_rejected(provider, choice):
    provider[0]["choices"] = [choice]
    assert proxy.completion(request())["error"] == "incomplete_response"


def test_rejected_response_keeps_only_valid_usage_counters(provider):
    provider[0]["choices"] = []
    provider[0]["usage"] = {"prompt_tokens": 10, "completion_tokens": -1,
                            "total_tokens": True, "provider_debug": "sensitive-provider-body"}
    result = proxy.completion(request())
    assert result["error"] == "incomplete_response"
    assert result["usage"] == {"prompt_tokens": 10}
    assert "sensitive-provider-body" not in str(result)


def test_transport_failure_hides_provider_details(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("synthetic-test-key sensitive-provider-body")
    monkeypatch.setattr(proxy.httpx, "post", fail)
    with pytest.raises(HTTPException) as error:
        proxy.completion(request())
    assert error.value.detail == "model transport failed: ReadTimeout"


@pytest.mark.parametrize("reported_model", ["unapproved-model", None, {"invalid": "identity"}])
def test_actual_model_identity_must_match_approval(provider, reported_model):
    provider[0]["model"] = reported_model
    provider[0]["choices"][0] = {"finish_reason": "tool_calls", "message": {
        "role": "assistant", "tool_calls": [{"id": "must-not-execute"}]}}
    result = proxy.completion(request())
    assert result["error"] == "model_identity_mismatch"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert not result["message"].get("tool_calls")
    assert len(provider[1]) == 1


def test_missing_model_identity_preserves_charge_and_stops(provider):
    del provider[0]["model"]
    result = proxy.completion(request())
    assert result["error"] == "model_identity_mismatch" and result["model"] is None
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
