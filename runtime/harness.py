import json
import os
import threading
import time

import httpx

from fastapi import FastAPI, HTTPException

from runtime.common import Journal, Run, Session, call, check_version

app = FastAPI(title="Agent harness", version="0")
journal = Journal("harness")
SANDBOX = os.getenv("SANDBOX_URL", "http://sandbox-manager:8000")
MODEL = os.getenv("MODEL_URL", "http://llm-proxy:8000")
guard = threading.RLock()
busy = {}
cancelled = set()


TOOLS = [{"type": "function", "function": {"name": name, "description": description,
    "parameters": {"type": "object", "properties": {p: {"type": "string"} for p in fields},
                   "required": fields, "additionalProperties": False}}}
    for name, description, fields in [
        ("ReadFile", "Read a UTF-8 file relative to /work.", ["path"]),
        ("WriteFile", "Write UTF-8 text to a file relative to /work.", ["path", "content"]),
        ("RunCommand", "Execute a command in /work. The result separately provides exit_code, stdout and stderr. Run existing programs directly without output merging or trailing status-print commands.", ["command"])]]


def finish(run_id, status, **detail):
    with guard:
        record = journal.get(run_id)
        if record["status"] != "running":
            return record
        record.update(status=status, **detail)
        journal.complete(run_id, run_id, record)
        busy.pop(record["session_id"], None)
        return record


@app.on_event("startup")
def recover():
    for key, record in journal.all():
        if record.get("status") == "running":
            try:
                call(SANDBOX, f"/sessions/{record['session_id']}/cancel", {})
            finally:
                finish(key, "error", error="harness_restarted")


@app.get("/health")
def health():
    call(SANDBOX, "/health")
    model = call(MODEL, "/health")
    return {"status": "ready", "version": "0", "model_mode": model["mode"], "model": model.get("model")}


@app.post("/sessions")
def provision(request: Session):
    check_version(request)
    return call(SANDBOX, "/sessions", request.model_dump())


def agent_loop(request):
    run_id = request.request_id
    started = time.monotonic()
    messages = [{"role": "system", "content":
        "Complete the task using tools. Files are in /work. Check command exit codes. "
        "Do not invent missing data. Run Python via python, scripts via sh. "
        "RunCommand already returns the exit code, stdout and stderr separately. "
        "When executing an existing program or test suite, run it directly in its own tool call. "
        "Do not combine that invocation with ls, cat, echo, postprocessing or output redirection. "
        "Use separate tool calls for inspection and subsequent file creation; preserve the program exit code. "
        "When the requested files have been written, return a final answer without tool calls."},
        {"role": "user", "content": request.instruction}]
    def deadline():
        with guard:
            cancelled.add(run_id)
        try:
            call(SANDBOX, f"/sessions/{request.session_id}/cancel", {})
        finally:
            finish(run_id, "error", error="timeout")
    timer = threading.Timer(request.timeout_seconds, deadline)
    timer.start()
    try:
        for step in range(request.max_steps):
            if run_id in cancelled:
                return
            response = call(MODEL, "/completion", {"messages": messages, "tools": TOOLS,
                "expected_mode": request.expected_mode, "expected_model": request.expected_model})
            if run_id in cancelled:
                return
            message = response["message"]
            journal.event(run_id, "model", step=step, usage=response.get("usage", {}),
                          model=response.get("model"), mode=response.get("mode"))
            if response.get("error"):
                finish(run_id, "error", error="model_error", detail=response["error"])
                return
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                finish(run_id, "success", answer=message.get("content") or "",
                       duration_seconds=time.monotonic()-started, model_mode=response.get("mode"))
                return
            if len(calls) > 16:
                raise ValueError("too many tool calls")
            for index, tool in enumerate(calls):
                if run_id in cancelled:
                    return
                name = tool["function"]["name"]
                args = json.loads(tool["function"]["arguments"])
                op = f"{run_id}-{step}-{index}"
                journal.event(run_id, "tool_call", operation_id=op, tool=name, arguments=args)
                try:
                    result = call(SANDBOX, "/exec", {"version": "0", "session_id": request.session_id,
                        "operation_id": op, "tool": name, "arguments": args, "timeout_seconds": 20})
                except HTTPException as exc:
                    if exc.status_code not in (400, 422):
                        raise
                    result = {"status": "error", "error": "invalid_tool", "detail": str(exc.detail)[:1000]}
                journal.event(run_id, "tool_result", operation_id=op, result=result)
                messages.append({"role": "tool", "tool_call_id": tool["id"], "content": json.dumps(result)})
                if result.get("error") in ("timeout", "sandbox_error"):
                    finish(run_id, "error", error=result["error"])
                    return
        finish(run_id, "error", error="step_limit")
    except Exception as exc:
        finish(run_id, "error", error="transport_error" if isinstance(exc, (HTTPException, OSError, httpx.HTTPError)) else "model_error",
               detail=type(exc).__name__)
    finally:
        timer.cancel()


@app.post("/runs")
def run(request: Run):
    check_version(request)
    if os.getenv("HARNESS", "sequential") != "sequential":
        raise HTTPException(503, "unsupported harness")
    with guard:
        try:
            existing = journal.get(request.request_id)
        except HTTPException:
            existing = None
        if existing is None:
            model = call(MODEL, "/health", timeout=5)
            if model.get("mode") != request.expected_mode:
                raise HTTPException(409, "configured model mode differs from requested mode")
            if request.expected_mode == "real" and (not request.expected_model or model.get("model") != request.expected_model):
                raise HTTPException(409, "configured model differs from requested model")
        if existing is None and request.session_id in busy:
            raise HTTPException(409, "one active run per session")
        fresh, record = journal.reserve(request.request_id, request.model_dump(),
            {"version": "0", "request_id": request.request_id, "session_id": request.session_id, "status": "running"})
        if fresh:
            busy[request.session_id] = request.request_id
            journal.event(request.request_id, "started", session_id=request.session_id)
            threading.Thread(target=agent_loop, args=(request,), daemon=True).start()
        return record


@app.get("/runs/{run_id}")
def status(run_id: str):
    return journal.get(run_id)


@app.get("/runs/{run_id}/events")
def events(run_id: str, after: int = 0):
    journal.get(run_id)
    return {"version": "0", "events": journal.events(run_id, after)}


@app.post("/runs/{run_id}/cancel")
def cancel(run_id: str):
    with guard:
        record = journal.get(run_id)
        if record["status"] != "running":
            return record
        cancelled.add(run_id)
    call(SANDBOX, f"/sessions/{record['session_id']}/cancel", {})
    return finish(run_id, "cancelled")


@app.get("/sessions/{session_id}/files")
def collect(session_id: str):
    return call(SANDBOX, f"/sessions/{session_id}/files")


@app.delete("/sessions/{session_id}")
def release(session_id: str):
    with guard:
        run_id = busy.get(session_id)
    if run_id:
        cancel(run_id)
    return call(SANDBOX, f"/sessions/{session_id}", method="DELETE")
