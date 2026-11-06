"""Export protocol schemas and enrich live HTTP route descriptions."""

import argparse
import copy
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

from runtime.common import Operation, Run, Session


VERSION = {"type": "string", "const": "0"}
ID = {"type": "string", "pattern": r"^[a-zA-Z0-9_-]{1,100}$"}
TEXT = {"type": "string"}
NONNEGATIVE = {"type": "number", "minimum": 0}


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def variants(*names):
    return {"oneOf": [ref(name) for name in names]}


def event(kind, fields):
    return obj({"version": VERSION, "seq": {"type": "integer", "minimum": 1}, "trace_id": ID,
                "time": NONNEGATIVE, "type": {"const": kind}, **fields})


def definitions():
    defs = {model.__name__: model.model_json_schema() for model in (Session, Run, Operation)}
    defs.update({
        "RunCommandArguments": obj({"command": TEXT}),
        "ReadFileArguments": obj({"path": TEXT}),
        "WriteFileArguments": obj({"path": TEXT, "content": TEXT}),
        "HealthResponse": obj({"version": VERSION, "status": {"const": "ready"}}),
        "ClientHealthResponse": obj({"version": VERSION, "status": {"const": "ready"},
            "model_mode": {"enum": ["mock", "real"]}, "model": {"type": ["string", "null"]}}),
        "CollectResponse": obj({"version": VERSION, "files": {
            "type": "object", "additionalProperties": {"type": "string", "contentEncoding": "base64"}}}),
        "ReleaseResponse": obj({"version": VERSION, "status": {"const": "released"}}),
        "CancelResponse": obj({"version": VERSION, "status": {"const": "cancelled"}}),
        "ValidationIssue": obj({"loc": {"type": "array", "items": {"type": ["string", "integer"]}},
            "msg": TEXT, "type": TEXT, "input": {}, "ctx": {"type": "object"}, "url": TEXT},
            ["loc", "msg", "type"]),
        "HTTPErrorResponse": obj({"detail": {"oneOf": [TEXT, {
            "type": "array", "items": ref("ValidationIssue")}]} }),
    })
    # Preserve exact runtime model schemas while also capturing argument checks
    # performed by the operation endpoint after Pydantic parses the request.
    defs["OperationRequest"] = {"allOf": [ref("Operation"), *[
        {"if": {"properties": {"tool": {"const": tool}}, "required": ["tool"]},
         "then": {"properties": {"arguments": ref(tool + "Arguments")}}}
        for tool in ("RunCommand", "ReadFile", "WriteFile")]]}
    session_fields = {"version": VERSION, "session_id": Session.model_json_schema()["properties"]["session_id"]}
    defs["SessionReady"] = obj({**session_fields, "status": {"const": "ready"}})
    defs["SessionInProgress"] = obj({**session_fields, "status": {"const": "running"}})
    defs["SessionReleased"] = obj({**session_fields, "status": {"const": "released"}})
    defs["SessionFailure"] = obj({**session_fields, "status": {"const": "error"},
        "error": {"const": "sandbox_error"}, "detail": TEXT},
        ["version", "session_id", "status", "error"])
    defs["SessionResponse"] = variants("SessionReady", "SessionInProgress", "SessionReleased", "SessionFailure")
    operation_fields = {"version": VERSION, "operation_id": ID}
    command_fields = {"exit_code": {"type": "integer"}, "stdout": TEXT, "stderr": TEXT,
                      "truncated": {"type": "boolean"}, "duration_seconds": NONNEGATIVE}
    defs["OperationInProgress"] = obj({**operation_fields, "status": {"const": "running"}})
    defs["OperationSuccess"] = obj({**operation_fields, "status": {"const": "success"},
                                    **command_fields, "exit_code": {"const": 0}})
    defs["OperationFailure"] = obj({**operation_fields, "status": {"const": "error"},
        "error": {"enum": ["command_error", "timeout", "sandbox_error"]},
        **command_fields, "exit_code": {"type": ["integer", "null"]}, "detail": TEXT},
        ["version", "operation_id", "status", "error"])
    defs["OperationFailure"]["allOf"] = [{
        "if": {"properties": {"error": {"const": "command_error"}}, "required": ["error"]},
        "then": {"required": list(command_fields),
                 "properties": {"exit_code": {"type": "integer", "not": {"const": 0}}}},
    }]
    defs["OperationResult"] = variants("OperationSuccess", "OperationFailure")
    defs["OperationResponse"] = variants("OperationInProgress", "OperationSuccess", "OperationFailure")
    run_fields = {"version": VERSION, "request_id": Run.model_json_schema()["properties"]["request_id"], "session_id": session_fields["session_id"]}
    defs["RunInProgress"] = obj({**run_fields, "status": {"const": "running"}})
    defs["RunSuccess"] = obj({**run_fields, "status": {"const": "success"}, "answer": TEXT,
        "duration_seconds": NONNEGATIVE, "model_mode": {"enum": ["mock", "real"]}})
    defs["RunFailure"] = obj({**run_fields, "status": {"const": "error"},
        "error": {"enum": ["harness_restarted", "timeout", "sandbox_error", "step_limit", "transport_error", "model_error"]},
        "detail": TEXT}, ["version", "request_id", "session_id", "status", "error"])
    defs["RunCancelled"] = obj({**run_fields, "status": {"const": "cancelled"}})
    defs["RunResult"] = variants("RunSuccess", "RunFailure", "RunCancelled")
    defs["RunResponse"] = variants("RunInProgress", "RunSuccess", "RunFailure", "RunCancelled")
    defs["InvalidToolResult"] = obj({"status": {"const": "error"}, "error": {"const": "invalid_tool"}, "detail": TEXT})
    defs["StartedEvent"] = event("started", {"session_id": session_fields["session_id"]})
    defs["ModelEvent"] = event("model", {"step": {"type": "integer", "minimum": 0},
        "usage": {"type": "object", "description": "Provider-native token usage; an empty object means unavailable."},
        "model": {"type": ["string", "null"]}, "mode": {"enum": ["mock", "real"]}})
    defs["ToolCallEvent"] = event("tool_call", {"operation_id": ID,
        "tool": {"type": "string", "description": "Raw model-selected name, recorded before validation."},
        "arguments": {"description": "Raw JSON arguments, recorded before tool schema validation."}})
    defs["ToolResultEvent"] = event("tool_result", {"operation_id": ID,
        "result": {"oneOf": [ref("OperationResponse"), ref("InvalidToolResult")]}})
    defs["ClientResultEvent"] = event("result", {"result": ref("RunResult")})
    defs["SandboxResultEvent"] = event("result", {"result": ref("OperationResult")})
    defs["ClientEvent"] = variants("StartedEvent", "ModelEvent", "ToolCallEvent", "ToolResultEvent", "ClientResultEvent")
    defs["SandboxEvent"] = variants("StartedEvent", "SandboxResultEvent")
    for protocol in ("Client", "Sandbox"):
        defs[protocol + "EventBatch"] = obj({"version": VERSION,
            "events": {"type": "array", "items": ref(protocol + "Event")}})
    return defs


def schemas():
    defs = definitions()
    groups = {
        "client-v0": ["Session", "Run", "SessionResponse", "RunResponse", "ClientEventBatch", "ClientHealthResponse",
                      "CollectResponse", "ReleaseResponse", "HTTPErrorResponse"],
        "sandbox-v0": ["Session", "OperationRequest", "SessionResponse", "OperationResponse", "SandboxEventBatch",
                       "HealthResponse", "CollectResponse", "CancelResponse", "ReleaseResponse", "HTTPErrorResponse"],
    }
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:itmo:{name}",
            "$defs": copy.deepcopy(defs), "oneOf": [ref(item) for item in items]} for name, items in groups.items()}


ROUTES = {
    "client-v0": {
        ("/health", "get"): (None, "ClientHealthResponse"),
        ("/sessions", "post"): ("Session", "SessionResponse"),
        ("/runs", "post"): ("Run", "RunResponse"),
        ("/runs/{run_id}", "get"): (None, "RunResponse"),
        ("/runs/{run_id}/events", "get"): (None, "ClientEventBatch"),
        ("/runs/{run_id}/stream", "get"): (None, "ClientEvent"),
        ("/runs/{run_id}/cancel", "post"): (None, "RunResponse"),
        ("/sessions/{session_id}/files", "get"): (None, "CollectResponse"),
        ("/sessions/{session_id}", "delete"): (None, "ReleaseResponse"),
    },
    "sandbox-v0": {
        ("/health", "get"): (None, "HealthResponse"),
        ("/sessions", "post"): ("Session", "SessionResponse"),
        ("/exec", "post"): ("OperationRequest", "OperationResponse"),
        ("/operations/{operation_id}/events", "get"): (None, "SandboxEventBatch"),
        ("/operations/{operation_id}/stream", "get"): (None, "SandboxEvent"),
        ("/sessions/{session_id}/files", "get"): (None, "CollectResponse"),
        ("/sessions/{session_id}/cancel", "post"): (None, "CancelResponse"),
        ("/sessions/{session_id}", "delete"): (None, "ReleaseResponse"),
    },
}


def openapi_refs(value):
    if isinstance(value, dict):
        return {key: (item.replace("#/$defs/", "#/components/schemas/") if key == "$ref" else openapi_refs(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [openapi_refs(item) for item in value]
    return value


def enrich_openapi(raw, protocol):
    result = copy.deepcopy(raw)
    actual = {(path, method) for path, item in raw["paths"].items()
              for method in item if method in {"get", "post", "delete", "put", "patch"}}
    if actual != set(ROUTES[protocol]):
        raise ValueError(f"Live route mismatch for {protocol}: {actual ^ set(ROUTES[protocol])}")
    result["info"]["title"] = "Client server protocol" if protocol == "client-v0" else "Harness sandbox protocol"
    result["info"]["version"] = "0"
    result["x-live-source"] = {"service": "harness" if protocol == "client-v0" else "sandbox-manager",
        "sha256": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
        "response_schemas": "Enriched from the executable v0 contract; live routes and request models are preserved."}
    components = result.setdefault("components", {})
    components.setdefault("schemas", {}).update(openapi_refs(definitions()))
    if protocol == "client-v0":
        components.setdefault("securitySchemes", {})["ClientToken"] = {"type": "http", "scheme": "bearer"}
    for (path, method), (request, response) in ROUTES[protocol].items():
        route = result["paths"][path][method]
        if request:
            route["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{request}"}}}}
        route["responses"] = {"200": {"description": "Protocol response",
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}}}
        for code, description in [("400", "Malformed request"), ("404", "Unknown resource"),
                ("409", "Identifier conflict or busy session"), ("413", "Payload too large"),
                ("422", "Request validation failed"), ("502", "Upstream transport failed"),
                ("503", "Configuration unavailable")]:
            route["responses"][code] = {"description": description, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPErrorResponse"}}}}
        route["responses"]["500"] = {"description": "Service failure; not an accepted operation result",
            "content": {"text/plain": {"schema": {"type": "string"}},
                        "application/json": {"schema": {"$ref": "#/components/schemas/HTTPErrorResponse"}}}}
        if path.endswith("/stream"):
            route["responses"]["200"] = {"description": "Server-sent event frames with id, event, and JSON data fields",
                "content": {"text/event-stream": {"schema": {"type": "string"},
                    "x-event-schema": {"$ref": f"#/components/schemas/{response}"}}}}
            route.setdefault("parameters", []).append({"name": "Last-Event-ID", "in": "header", "required": False,
                "schema": {"type": "string", "pattern": "^-?[0-9]+$"},
                "description": "Last delivered sequence number; takes precedence over the after query parameter."})
        if protocol == "client-v0" and path != "/health":
            route["security"] = [{"ClientToken": []}]
            route["responses"]["401"] = {"description": "Invalid Bearer token", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/HTTPErrorResponse"}}}}
    return result


def fetch_openapi(service, compose_command):
    script = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json',timeout=10).read().decode())"
    command = [*shlex.split(compose_command), "exec", "-T", service, "python", "-c", script]
    return json.loads(subprocess.check_output(command, text=True, timeout=30))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also export routes from the running Compose services")
    parser.add_argument("--compose-command", default="docker-compose")
    args = parser.parse_args()
    root = Path("contracts")
    root.mkdir(exist_ok=True)
    for name, schema in schemas().items():
        (root / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
        if args.live:
            service = "harness" if name == "client-v0" else "sandbox-manager"
            raw = fetch_openapi(service, args.compose_command)
            if name == "client-v0":
                gateway = fetch_openapi("gateway", args.compose_command)
                raw["paths"]["/runs/{run_id}/stream"] = gateway["paths"]["/runs/{run_id}/stream"]
                raw["paths"]["/health"] = gateway["paths"]["/health"]
            document = enrich_openapi(raw, name)
            if name == "client-v0":
                document["x-live-source"]["service"] = "harness plus gateway stream and health"
            (root / f"{name}.openapi.json").write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
