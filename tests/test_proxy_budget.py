"""Verify the spend gate without sending model or billing requests."""

import os

import httpx
import pytest
from fastapi import HTTPException

from runtime import proxy
from runtime.budget import BudgetLedger


@pytest.fixture
def account(monkeypatch, tmp_path):
    for key, value in {
        "MODEL_MODE": "real",
        "MODEL_BASE_URL": proxy.OPENROUTER_BASE,
        "MODEL_NAME": proxy.OPENROUTER_MODEL,
        "MODEL_API_KEY": "synthetic-key",
        "MODEL_BUDGET_PATH": str(tmp_path / "budget.sqlite3"),
        "MODEL_BUDGET_ID": "approved-test-budget",
        "MODEL_BUDGET_USD": "1",
    }.items():
        monkeypatch.setenv(key, value)
    BudgetLedger(os.environ["MODEL_BUDGET_PATH"], "approved-test-budget", "1")
    calls = []
    reply = {
        "model": proxy.OPENROUTER_MODEL,
        "provider": "DeepInfra",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cost": 0.0000011,
            "is_byok": False,
        },
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Done"},
            }
        ],
    }

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(proxy.httpx, "post", post)
    return reply, calls


def body(**extra):
    return {
        "expected_mode": "real",
        "expected_model": proxy.OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": "Read the file"}],
        "tools": [],
        **extra,
    }


def test_provider_profile_and_cost_are_pinned(account):
    result = proxy.completion(body(model="unapproved", plugins=[{"id": "web"}], n=10))
    sent = account[1][0]
    assert sent["model"] == proxy.OPENROUTER_MODEL
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["max_tokens"] == 4096
    assert sent["provider"] == {
        "only": ["deepinfra/fp8"],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "require_parameters": True,
        "max_price": {"prompt": 0.3, "completion": 1.2, "request": 0},
    }
    assert "plugins" not in sent and "n" not in sent
    assert result["usage"]["cost_usd"] == 0.000002
    assert proxy.budget_status()["charged_micro"] == 2
    assert proxy.budget_status()["reserved_micro"] == 0


@pytest.mark.parametrize(
    "name", ["MODEL_BUDGET_ID", "MODEL_BUDGET_USD", "MODEL_BUDGET_PATH"]
)
def test_missing_config_never_calls_provider(account, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert account[1] == []


def test_insufficient_budget_never_calls_provider(account, monkeypatch):
    monkeypatch.setenv("MODEL_BUDGET_USD", "0.34")
    monkeypatch.setenv("MODEL_BUDGET_ID", "limited")
    BudgetLedger(os.environ["MODEL_BUDGET_PATH"], "limited", "0.34")
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert account[1] == []
    assert proxy.budget_status()["reserved_micro"] == 0


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"cost": 0},
        {"cost": -1, "is_byok": False},
        {"cost": True, "is_byok": False},
        {"cost": "NaN", "is_byok": False},
    ],
)
def test_unknown_accounting_retains_reservation(account, usage):
    account[0]["usage"] = usage
    result = proxy.completion(body())
    assert "cost_usd" not in result["usage"]
    assert proxy.budget_status()["reserved_micro"] == proxy.REQUEST_RESERVE_MICRO


def test_transport_error_retains_reservation(account, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("synthetic failure")

    monkeypatch.setattr(proxy.httpx, "post", fail)
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert proxy.budget_status()["reserved_micro"] == proxy.REQUEST_RESERVE_MICRO


@pytest.mark.parametrize("change", ["model", "provider", "byok", "overcharge"])
def test_contract_violation_blocks_later_calls(account, change):
    if change in ("model", "provider"):
        account[0][change] = "unexpected"
    elif change == "byok":
        account[0]["usage"]["is_byok"] = True
    else:
        account[0]["usage"]["cost"] = 0.36
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert proxy.budget_status()["blocked"] is True
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert len(account[1]) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://invalid"}}
                    ],
                }
            ]
        },
        {"tools": [{"type": "web_search"}]},
        {"messages": [{"role": "user", "content": "x" * (256 * 1024)}]},
    ],
)
def test_out_of_profile_payload_never_calls_provider(account, extra):
    with pytest.raises(HTTPException):
        proxy.completion(body(**extra))
    assert account[1] == []
    assert proxy.budget_status()["reserved_micro"] == 0


def test_two_unknown_calls_cannot_authorize_third(account):
    account[0]["usage"] = {}
    proxy.completion(body())
    proxy.completion(body())
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert len(account[1]) == 2
    assert proxy.budget_status()["reserved_micro"] == 700000


def test_mock_does_not_open_budget_storage(account, monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "mock")
    monkeypatch.setenv("MODEL_BUDGET_PATH", "/nonexistent/budget.sqlite3")
    proxy.completion(body(expected_mode="mock", expected_model=None))
    assert proxy.budget_status() == {"enabled": False}
    assert account[1] == []


@pytest.mark.parametrize(
    "address",
    [
        "https://openrouter.ai:443/api/v1",
        "https://OPENROUTER.AI/api/v1/",
        "https://openrouter.ai./api/v1",
    ],
)
def test_equivalent_address_cannot_bypass_budget(account, monkeypatch, address):
    monkeypatch.setenv("MODEL_BASE_URL", address)
    monkeypatch.setenv("MODEL_BUDGET_ID", "zero-budget")
    monkeypatch.setenv("MODEL_BUDGET_USD", "0")
    BudgetLedger(os.environ["MODEL_BUDGET_PATH"], "zero-budget", "0")
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert account[1] == []


@pytest.mark.parametrize(
    "address",
    [
        "https://openrouter.ai:444/api/v1",
        "https://openrouter.ai/api/v1?x=1",
        "https://eu.openrouter.ai/api/v1",
    ],
)
def test_unsupported_openrouter_address_cannot_bypass_budget(
    account, monkeypatch, address
):
    monkeypatch.setenv("MODEL_BASE_URL", address)
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert account[1] == []


def test_refusal_metadata_does_not_break_following_call(account):
    message = account[0]["choices"][0]["message"]
    message.update(refusal=None, annotations=[])
    first = proxy.completion(body())
    proxy.completion(
        body(messages=[first["message"], {"role": "user", "content": "Continue"}])
    )
    assert len(account[1]) == 2
    assert "refusal" not in account[1][1]["messages"][0]
    assert "annotations" not in account[1][1]["messages"][0]


def test_deleted_storage_does_not_reset_approved_budget(account):
    from pathlib import Path

    Path(os.environ["MODEL_BUDGET_PATH"]).unlink()
    with pytest.raises(HTTPException):
        proxy.completion(body())
    assert account[1] == []
    assert not Path(os.environ["MODEL_BUDGET_PATH"]).exists()
