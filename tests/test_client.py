import base64
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import jsonschema
import pytest

from client.cli import Client
from runtime.common import Operation, Run, Session


def test_demo_independent_file_and_cli(tmp_path):
    fixture = tmp_path / "fixture"
    (fixture / "config").mkdir(parents=True)
    (fixture / "config/app.yaml").write_text("server:\n  port: 4917\n")
    state, trace = Client().execute(fixture, "Read the port from config/app.yaml into answer.txt", tmp_path / "output")
    assert state["status"] == "success"
    assert (tmp_path / "output/answer.txt").read_text().strip() == "4917"
    assert len([e for e in trace if e["type"] == "result"]) == 1
    assert [e["tool"] for e in trace if e["type"] == "tool_call"] == ["ReadFile", "WriteFile"]


def test_auth():
    url = os.environ.get("RUNTIME_URL", "http://127.0.0.1:8080")
    response = httpx.post(url + "/sessions", json={}, trust_env=False)
    assert response.status_code == 401


def test_resume_and_idempotency():
    client = Client()
    session, request = uuid.uuid4().hex, uuid.uuid4().hex
    client.request("POST", "/sessions", {"session_id": session, "files": {"config/app.yaml": base64.b64encode(b"port: 9876\n").decode()}})
    payload = {"request_id": request, "session_id": session, "instruction": "Read the port and write answer.txt"}
    try:
        client.request("POST", "/runs", payload)
        client.request("POST", "/runs", payload)
        deadline = time.monotonic()+30
        while client.request("GET", f"/runs/{request}")["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(.1)
        events = Client().request("GET", f"/runs/{request}/events")["events"]
        assert len([e for e in events if e["type"] == "result"]) == 1
        with client.http.stream("GET", f"/runs/{request}/stream", headers={"Last-Event-ID": str(events[0]["seq"])}) as response:
            streamed = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
        assert streamed == events[1:]
        assert len([e for e in events if e["type"] == "tool_call"]) == 2
        assert Client().request("GET", f"/runs/{request}/events?after={events[0]['seq']}")["events"] == events[1:]
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.request("POST", "/runs", {**payload, "instruction": "different"})
        assert error.value.response.status_code == 409
    finally:
        client.request("DELETE", f"/sessions/{session}")


def test_schemas_match_runtime():
    for name, models in [("client-v0", [Session, Run]), ("sandbox-v0", [Session, Operation])]:
        schema = json.loads(Path(f"contracts/{name}.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        for model in models:
            assert schema["$defs"][model.__name__] == model.model_json_schema()
    jsonschema.validate({"version": "0", "session_id": "s", "request_id": "r", "instruction": "hello"},
                        json.loads(Path("contracts/client-v0.schema.json").read_text()))


@pytest.mark.parametrize("task_id", ["B1.1", "B2.1", "B3.1", "B4.1", "B5.1"])
def test_replayed_baf_smoke(task_id, tmp_path):
    from baf.run import run_task
    task = next(p for p in Path("baf").glob("B[1-5]-*/B*/task.yaml") if p.parent.name.startswith(task_id + "-"))
    result = run_task(task, tmp_path, Client(), mode="mock")
    assert result["pass"] is True, result
    assert result["modes"] == ["mock"]
