import json
import os
from decimal import Decimal
from urllib.parse import urlsplit

from runtime.budget import BudgetError, BudgetLedger

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Model proxy", version="0")


OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash-1017"
OPENROUTER_PROVIDER = "deepinfra/fp8"
OPENROUTER_PROVIDER_NAME = "DeepInfra"
REQUEST_RESERVE_MICRO = 350_000


def request_budget():
    """Open the existing spending ledger for the configured provider profile."""
    if os.getenv("MODEL_MODE", "mock") != "real":
        return None
    try:
        address = urlsplit(os.getenv("MODEL_BASE_URL", ""))
        host = (address.hostname or "").rstrip(".").lower()
        if host != "openrouter.ai" and not host.endswith(".openrouter.ai"):
            return None
        if (host != "openrouter.ai" or address.scheme != "https" or address.port not in (None, 443)
                or address.path.rstrip("/") != "/api/v1" or address.query or address.fragment
                or address.username is not None or address.password is not None):
            raise ValueError("unsupported OpenRouter address")
    except ValueError:
        raise HTTPException(503, "unsupported OpenRouter address") from None
    if os.getenv("MODEL_NAME") != OPENROUTER_MODEL:
        raise HTTPException(503, "OpenRouter model has no reviewed budget profile")
    try:
        return BudgetLedger(os.environ["MODEL_BUDGET_PATH"],
                            os.environ["MODEL_BUDGET_ID"], os.environ["MODEL_BUDGET_USD"], initialize=False)
    except (KeyError, ValueError, BudgetError, OSError):
        raise HTTPException(503, "OpenRouter budget configuration is unavailable") from None


@app.get("/budget")
def budget_status():
    ledger = request_budget()
    return ledger.snapshot() if ledger is not None else {"enabled": False}


def restrict_openrouter(payload):
    # Only text and client-side functions are covered by this cost bound.
    if not isinstance(payload["messages"], list) or not all(
        isinstance(m, dict) and m.get("role") in ("system", "user", "assistant", "tool")
        and (m.get("content") is None or isinstance(m["content"], str))
        and set(m) <= {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning", "reasoning_details", "reasoning_content", "refusal", "annotations"}
        for m in payload["messages"]
    ):
        raise HTTPException(422, "budget profile requires text-only messages")
    if not isinstance(payload["tools"], list) or not all(
        isinstance(t, dict) and t.get("type") == "function" and set(t) <= {"type", "function"}
        for t in payload["tools"]
    ):
        raise HTTPException(422, "budget profile requires client-side function tools")
    payload["messages"] = [{k: v for k, v in m.items() if k not in {"refusal", "annotations"}}
                           for m in payload["messages"]]
    payload["reasoning"] = {"effort": "high"}
    payload["provider"] = {"only": [OPENROUTER_PROVIDER], "allow_fallbacks": False, "data_collection": "deny",
        "require_parameters": True, "max_price": {"prompt": 0.3, "completion": 1.2, "request": 0}}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise HTTPException(422, "request exceeds reviewed budget profile size")


@app.get("/health")
def health():
    mode = os.getenv("MODEL_MODE", "mock")
    if mode not in ("mock", "real"):
        raise HTTPException(503, "unknown model mode")
    if mode == "real" and not all(os.getenv(k) for k in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")):
        raise HTTPException(503, "real model configuration is incomplete")
    request_budget()
    return {"status": "ready", "mode": mode,
            "model": os.getenv("MODEL_NAME") if mode == "real" else None}


@app.post("/completion")
def completion(body: dict):
    config = health()
    expected_mode = body.get("expected_mode", "mock")
    if expected_mode not in ("mock", "real"):
        raise HTTPException(422, "unknown expected model mode")
    if config["mode"] != expected_mode:
        raise HTTPException(409, "runtime mode differs from requested run mode")
    if expected_mode == "real" and (
        not isinstance(body.get("expected_model"), str)
        or not body["expected_model"].strip()
        or body["expected_model"] != config["model"]
    ):
        raise HTTPException(409, "configured model differs from approved evidence")
    if config["mode"] == "mock":
        from runtime.replay import response
        replay = response(body["messages"])
        if replay is not None:
            return replay
        # Deliberately fixed demo replay; it is never scored as model quality.
        observations = [m for m in body["messages"] if m["role"] == "tool"]
        step = len(observations)
        if step == 0:
            name, args = "ReadFile", {"path": "config/app.yaml"}
        elif step == 1:
            import re
            observation = json.loads(observations[-1]["content"])
            match = re.search(r"port:\s*(\d+)", observation.get("stdout", ""))
            if not match:
                return {"message": {"role": "assistant", "content": "No port found"}, "usage": {}, "mode": "mock"}
            name, args = "WriteFile", {"path": "answer.txt", "content": match[1] + "\n"}
        else:
            return {"message": {"role": "assistant", "content": "Finished"}, "usage": {}, "mode": "mock"}
        return {"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"mock-{step}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
            "usage": {}, "mode": "mock"}
    base = os.environ["MODEL_BASE_URL"]
    if not base.startswith("https://"):
        raise HTTPException(503, "model provider must use HTTPS")
    payload = {"model": os.environ["MODEL_NAME"], "messages": body["messages"],
               "tools": body["tools"], "temperature": 0, "max_tokens": 4096}
    ledger = request_budget()
    reservation = None
    if ledger is not None:
        base = OPENROUTER_BASE
        restrict_openrouter(payload)
        try:
            reservation = ledger.reserve(REQUEST_RESERVE_MICRO)
        except BudgetError:
            raise HTTPException(503, "model budget exhausted or blocked") from None
    try:
        response = httpx.post(base.rstrip("/") + "/chat/completions", json=payload,
                             headers={"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]},
                             timeout=60, trust_env=False)
        response.raise_for_status()
        value = response.json(parse_float=Decimal) if ledger is not None else response.json()
    except Exception as exc:
        # Provider bodies can contain credentials and must not enter client traces.
        raise HTTPException(502, "model transport failed: " + type(exc).__name__)
    if not isinstance(value, dict):
        raise HTTPException(502, "invalid model response")
    cost_micro = None
    if ledger is not None:
        native_usage = value.get("usage")
        native_usage = native_usage if isinstance(native_usage, dict) else {}
        if value.get("model") != OPENROUTER_MODEL or value.get("provider") != OPENROUTER_PROVIDER_NAME:
            ledger.block("unexpected_model_or_provider")
            raise HTTPException(502, "model budget provider identity mismatch")
        if native_usage.get("is_byok") is True:
            ledger.block("external_provider_billing")
            raise HTTPException(502, "external provider billing is outside budget profile")
        # Missing billing provenance must never turn an uncertain request into free use.
        cost = native_usage.get("cost") if native_usage.get("is_byok") is False else None
        settled = ledger.settle(reservation, cost)
        if settled.get("violation"):
            raise HTTPException(502, "provider cost exceeded reserved bound")
        cost_micro = settled.get("charged_micro")
    # Retain safe usage counters even when the charged response is incomplete.
    usage = value.get("usage", {})
    usage = {key: count for key, count in usage.items()
             if key in ("prompt_tokens", "completion_tokens", "total_tokens")
             and type(count) is int and count >= 0} if isinstance(usage, dict) else {}
    if cost_micro is not None:
        usage["cost_usd"] = cost_micro / 1_000_000
    reported_model = value.get("model")
    result = {"usage": usage, "model": reported_model if isinstance(reported_model, str) else None,
              "mode": "real"}
    if result["model"] != config["model"]:
        return {**result, "message": {"role": "assistant", "content": None},
                "error": "model_identity_mismatch"}
    choices = value.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message")
    finish_reason = choice.get("finish_reason")
    if isinstance(message, dict):
        calls = message.get("tool_calls")
        valid = message.get("role") == "assistant" and (
            finish_reason == "stop" and not calls and isinstance(message.get("content"), str)
            or finish_reason == "tool_calls" and isinstance(calls, list) and bool(calls)
        )
    else:
        valid = False
    if not valid:
        return {**result, "message": {"role": "assistant", "content": None},
                "error": "incomplete_response",
                "finish_reason": finish_reason if finish_reason in ("stop", "tool_calls", "length", "content_filter") else "unknown"}
    return {**result, "message": message}
