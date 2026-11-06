"""Validate live protocol responses and exported schemas without model calls."""

import base64
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import jsonschema
import pytest

from runtime.common import Operation, Run, Session
from scripts.export_contracts import ROUTES, schemas


ROOT = Path("contracts")


def schema(protocol):
    return json.loads((ROOT / f"{protocol}.schema.json").read_text())


def validator(protocol, name):
    document = schema(protocol)
    return jsonschema.Draft202012Validator({"$schema": document["$schema"],
        "$defs": document["$defs"], "$ref": f"#/$defs/{name}"})


def validate(protocol, name, value):
    validator(protocol, name).validate(value)
    return value


def stream_events(client, path, protocol, event_schema, after=0):
    seen = []
    with client.stream("GET", path, headers={"Last-Event-ID": str(after)}) as response:
        assert response.status_code == 200, response.status_code
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = validate(protocol, event_schema, json.loads(line[6:]))
                seen.append(event)
                if event["type"] == "result":
                    break
    assert seen and seen[-1]["type"] == "result"
    return seen


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def check_response(client, method, path, protocol, name, body=None, expected=200, **kwargs):
    response = client.request(method, path, json=body, **kwargs)
    assert response.status_code == expected, response.text
    return validate(protocol, name, response.json())


def test_exports_match_runtime_and_cover_all_http_routes():
    exported = schemas()
    for protocol, models in [("client-v0", (Session, Run)), ("sandbox-v0", (Session, Operation))]:
        document = schema(protocol)
        assert document == exported[protocol]
        jsonschema.Draft202012Validator.check_schema(document)
        for model in models:
            assert document["$defs"][model.__name__] == model.model_json_schema()
        openapi = json.loads((ROOT / f"{protocol}.openapi.json").read_text())
        assert openapi["info"]["version"] == "0"
        assert openapi["x-live-source"]["sha256"]
        for (path, method), (_, result) in ROUTES[protocol].items():
            operation = openapi["paths"][path][method]
            if path.endswith("/stream"):
                assert "text/event-stream" in operation["responses"]["200"]["content"]
            else:
                assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
                    "$ref": f"#/components/schemas/{result}"}
            if protocol == "client-v0" and path != "/health":
                assert operation["security"] == [{"ClientToken": []}]


def test_schemas_reject_malformed_versions_statuses_results_and_arguments():
    good = {"version": "0", "operation_id": "o", "status": "success", "exit_code": 0,
            "stdout": "", "stderr": "", "truncated": False, "duration_seconds": 0.01}
    validate("sandbox-v0", "OperationResult", good)
    bad_responses = [{**good, "version": "1"}, {**good, "status": "invented"},
        {**good, "exit_code": 7}, {key: value for key, value in good.items() if key != "version"},
        {**good, "unexpected": True}, {"version": "0", "operation_id": "o", "status": "running"},
        {"version": "0", "operation_id": "o", "status": "error", "error": "invented"},
        {"version": "0", "operation_id": "o", "status": "error", "error": "command_error"}]
    for bad in bad_responses:
        assert not validator("sandbox-v0", "OperationResult").is_valid(bad), bad
    operation = {"version": "0", "operation_id": "o", "session_id": "s", "tool": "ReadFile",
                 "arguments": {"path": "input.txt"}, "timeout_seconds": 1}
    validate("sandbox-v0", "OperationRequest", operation)
    for args in ({"command": "ls"}, {"path": 12}, {"path": "input.txt", "unknown": True}):
        assert not validator("sandbox-v0", "OperationRequest").is_valid({**operation, "arguments": args})
    event = {"version": "0", "seq": 1, "trace_id": "o", "time": 1, "type": "result", "result": good}
    validate("sandbox-v0", "SandboxEvent", event)
    for changed in ({"version": "1"}, {"seq": 0}, {"result": {"version": "0", "status": "running", "operation_id": "o"}}):
        assert not validator("sandbox-v0", "SandboxEvent").is_valid({**event, **changed})
    assert not validator("client-v0", "RunResponse").is_valid({"version": "0", "request_id": "r", "session_id": "s", "status": "success"})
    assert not validator("client-v0", "ClientEventBatch").is_valid({"events": []})


def test_live_sandbox_responses_errors_cancel_release_and_events():
    url = os.environ.get("SANDBOX_URL", "http://sandbox-manager:8000")
    protocol = "sandbox-v0"
    session = identifier("schema-sb")
    with httpx.Client(base_url=url, trust_env=False, timeout=40) as client:
        check_response(client, "GET", "/health", protocol, "HealthResponse")
        check_response(client, "POST", "/sessions", protocol, "SessionResponse", {
            "version": "0", "session_id": session, "files": {"input.txt": base64.b64encode(b"value").decode()}})
        try:
            check_response(client, "GET", f"/sessions/{session}/files", protocol, "CollectResponse")
            for command in ("printf value", "printf OK; exit 7"):
                operation = identifier("schema-op")
                request = {"version": "0", "operation_id": operation, "session_id": session,
                    "tool": "RunCommand", "arguments": {"command": command}, "timeout_seconds": 2}
                check_response(client, "POST", "/exec", protocol, "OperationResponse", request)
                batch = check_response(client, "GET", f"/operations/{operation}/events", protocol, "SandboxEventBatch")
                assert len([event for event in batch["events"] if event["type"] == "result"]) == 1
                streamed = stream_events(client, f"/operations/{operation}/stream", protocol, "SandboxEvent")
                assert streamed == batch["events"]
                for malformed in ({**request, "version": "1"}, {**request, "arguments": {"command": 3}}):
                    check_response(client, "POST", "/exec", protocol, "HTTPErrorResponse", malformed, expected=422)
            check_response(client, "POST", f"/sessions/{session}/cancel", protocol, "CancelResponse", {})
            check_response(client, "GET", f"/sessions/{session}/files", protocol, "HTTPErrorResponse", expected=404)
        finally:
            check_response(client, "DELETE", f"/sessions/{session}", protocol, "ReleaseResponse")


def test_live_client_responses_events_reconnect_and_cancel():
    url = os.environ.get("RUNTIME_URL", "http://127.0.0.1:8080")
    token = os.environ.get("CLIENT_TOKEN", "local-checkpoint-token")
    protocol = "client-v0"
    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=40) as client:
        check_response(client, "GET", "/health", protocol, "ClientHealthResponse")
        check_response(client, "POST", "/sessions", protocol, "HTTPErrorResponse",
            {"version": "1", "session_id": identifier("invalid")}, expected=422)
        session, run = identifier("schema-client"), identifier("schema-run")
        check_response(client, "POST", "/sessions", protocol, "SessionResponse", {
            "version": "0", "session_id": session,
            "files": {"config/app.yaml": base64.b64encode(b"port: 8127\n").decode()}})
        try:
            check_response(client, "POST", "/runs", protocol, "RunResponse", {
                "version": "0", "request_id": run, "session_id": session,
                "instruction": "Read the port and write answer.txt"})
            deadline = time.monotonic() + 30
            while True:
                state = check_response(client, "GET", f"/runs/{run}", protocol, "RunResponse")
                if state["status"] != "running":
                    break
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert state["status"] == "success", state
            batch = check_response(client, "GET", f"/runs/{run}/events", protocol, "ClientEventBatch")
            final = [event for event in batch["events"] if event["type"] == "result"]
            assert len(final) == 1 and final[0]["result"] == state
            resumed = check_response(client, "GET", f"/runs/{run}/events?after={batch['events'][0]['seq']}", protocol, "ClientEventBatch")
            assert resumed["events"] == batch["events"][1:]
            streamed = stream_events(client, f"/runs/{run}/stream", protocol, "ClientEvent", batch["events"][0]["seq"])
            assert streamed == resumed["events"]
            check_response(client, "GET", f"/sessions/{session}/files", protocol, "CollectResponse")
            # Cancelling a finished run returns its existing terminal state.
            terminal = check_response(client, "POST", f"/runs/{run}/cancel", protocol, "RunResponse", {})
            assert terminal == state
            check_response(client, "GET", "/runs/unknown-schema-run", protocol, "HTTPErrorResponse", expected=404)
        finally:
            check_response(client, "DELETE", f"/sessions/{session}", protocol, "ReleaseResponse")
    with httpx.Client(base_url=url, trust_env=False, timeout=10) as anonymous:
        check_response(anonymous, "GET", "/runs/unknown", protocol, "HTTPErrorResponse", expected=401)
