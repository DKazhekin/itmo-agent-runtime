import base64
import io
import json
import os
import socket
import tarfile
import threading
import time

import docker
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from runtime.streaming import stream_events

from runtime.common import Journal, Operation, Session, check_version

app = FastAPI(title="Harness sandbox protocol", version="0")
journal = Journal("sandbox")
engine = docker.from_env(timeout=15)
containers = {}
guard = threading.RLock()
active = set()
IMAGE = os.environ.get("SANDBOX_IMAGE", "itmo-sandbox:0.1")
LABEL = "itmo.checkpoint.sandbox"
NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "itmo-checkpoint")


@app.on_event("startup")
def recover():
    # A reserved operation is never executed again after an uncertain crash.
    for container in engine.containers.list(all=True, filters={"label": LABEL + "=" + NAMESPACE}):
        container.remove(force=True)
    for key, record in journal.all():
        if key.startswith("session:") and record.get("status") in ("ready", "running"):
            journal.put(key, {"version": "0", "session_id": key[8:], "status": "released"})
            continue
        if record.get("status") == "running":
            record.update(status="error", error="sandbox_error", detail="manager restarted; outcome unknown")
            if key.startswith("op:"):
                journal.complete(key, key[3:], record)
            else:
                journal.put(key, record)


@app.on_event("shutdown")
def shutdown():
    for container in list(containers.values()):
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


@app.get("/health")
def health():
    engine.ping()
    engine.images.get(IMAGE)
    return {"status": "ready", "version": "0"}


@app.post("/sessions")
def provision(request: Session):
    check_version(request)
    for path, content in request.files.items():
        if path.startswith("/") or any(p in ("", ".", "..", ".git") for p in path.split("/")):
            raise HTTPException(422, "unsafe file path")
        try:
            base64.b64decode(content, validate=True)
        except ValueError:
            raise HTTPException(422, "invalid base64")
    if sum(len(v) for v in request.files.values()) > 8_000_000:
        raise HTTPException(413, "fixture too large")
    with guard:
        fresh, record = journal.reserve("session:" + request.session_id, request.model_dump(),
                                        {"version": "0", "status": "running", "session_id": request.session_id})
        if not fresh:
            return record
        container = engine.containers.run(
            IMAGE, ["sleep", "infinity"], detach=True, user="1000:1000",
            name=NAMESPACE + "-sb-" + request.session_id, labels={LABEL: NAMESPACE},
            network_mode="none", read_only=True, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], pids_limit=64,
            mem_limit="256m", memswap_limit="256m", nano_cpus=500_000_000,
            tmpfs={"/work": "rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=0700",
                   "/tmp": "rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000,mode=1777"},
            working_dir="/work", environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        containers[request.session_id] = container
        try:
            blob = io.BytesIO()
            with tarfile.open(fileobj=blob, mode="w") as archive:
                for path, value in request.files.items():
                    data = base64.b64decode(value)
                    info = tarfile.TarInfo(path)
                    info.size, info.uid, info.gid, info.mode = len(data), 1000, 1000, 0o600
                    archive.addfile(info, io.BytesIO(data))
            setup = engine.api.exec_create(container.id, ["/usr/local/bin/python", "-I", "-S", "-c", "import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/work',filter='data')"], stdin=True, user="1000:1000")
            channel = engine.api.exec_start(setup["Id"], socket=True)
            channel._sock.settimeout(10)
            channel._sock.sendall(blob.getvalue())
            channel._sock.shutdown(socket.SHUT_WR)
            while channel.read(65536):
                pass
            channel.close()
            if engine.api.exec_inspect(setup["Id"])["ExitCode"] != 0:
                raise RuntimeError("fixture setup failed")
        except Exception:
            container.remove(force=True)
            containers.pop(request.session_id, None)
            journal.put("session:" + request.session_id, {"version": "0", "session_id": request.session_id, "status": "error", "error": "sandbox_error"})
            raise
        record["status"] = "ready"
        journal.put("session:" + request.session_id, record)
        return record


def get_container(session_id):
    with guard:
        if session_id not in containers:
            raise HTTPException(404, "sandbox absent or released")
        return containers[session_id]


def destroy(session_id):
    with guard:
        container = containers.get(session_id)
    if container:
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        with guard:
            if containers.get(session_id) is container:
                containers.pop(session_id)
    try:
        journal.put("session:" + session_id, {"version": "0", "session_id": session_id, "status": "released"})
    except Exception:
        pass


@app.delete("/sessions/{session_id}")
def release(session_id: str):
    destroy(session_id)
    return {"version": "0", "status": "released"}


@app.post("/sessions/{session_id}/cancel")
def cancel(session_id: str):
    destroy(session_id)
    return {"version": "0", "status": "cancelled"}


FILE_TOOL = '''import base64,json,pathlib,sys
a=json.loads(sys.argv[1]); p=(pathlib.Path('/work')/a['path']).resolve()
if not p.is_relative_to('/work') or '.git' in p.parts: raise ValueError('path outside allowed workdir')
if sys.argv[2]=='ReadFile':
 if p.stat().st_size>131072: raise ValueError('file too large')
 print(p.read_text())
else:
 p.parent.mkdir(parents=True,exist_ok=True); p.write_text(a['content']); print('written')
'''


@app.post("/exec")
def execute(request: Operation):
    check_version(request)
    allowed = {"RunCommand": {"command"}, "ReadFile": {"path"}, "WriteFile": {"path", "content"}}
    if set(request.arguments) != allowed[request.tool] or not all(isinstance(x, str) for x in request.arguments.values()):
        raise HTTPException(422, "invalid tool arguments")
    if sum(len(x) for x in request.arguments.values()) > 131072:
        raise HTTPException(413, "arguments too large")
    started = time.monotonic()
    with guard:
        fresh, record = journal.reserve("op:" + request.operation_id, request.model_dump(),
            {"version": "0", "operation_id": request.operation_id, "status": "running"})
        if not fresh:
            return record
        if request.session_id in active:
            record.update(status="error", error="sandbox_error", detail="session busy")
            journal.complete("op:" + request.operation_id, request.operation_id, record)
            return record
        active.add(request.session_id)
    journal.event(request.operation_id, "started", session_id=request.session_id)
    expired = threading.Event()
    timer = None
    try:
        container = get_container(request.session_id)
        def expire():
            expired.set()
            destroy(request.session_id)
        timer = threading.Timer(request.timeout_seconds, expire)
        timer.start()
        cmd = ["/bin/sh", "-c", request.arguments["command"]] if request.tool == "RunCommand" else [
            "/usr/local/bin/python", "-I", "-S", "-c", FILE_TOOL, json.dumps(request.arguments), request.tool]
        execution = engine.api.exec_create(container.id, cmd, workdir="/work", user="1000:1000")
        chunks, size = [], 0
        for chunk in engine.api.exec_start(execution["Id"], stream=True, demux=True):
            for index, data in enumerate(chunk):
                if data:
                    if size < 131072:
                        chunks.append((index, data[:131072-size]))
                    size += len(data)
                    if size > 2_000_000:
                        destroy(request.session_id)
                        raise RuntimeError("output limit exceeded")
        exit_code = engine.api.exec_inspect(execution["Id"])["ExitCode"]
        record.update(status="success" if exit_code == 0 else "error", exit_code=exit_code,
                      stdout=b"".join(d for i, d in chunks if i == 0).decode(errors="replace"),
                      stderr=b"".join(d for i, d in chunks if i == 1).decode(errors="replace"),
                      truncated=size > 131072)
        if exit_code != 0:
            record["error"] = "command_error"
    except Exception as exc:
        record.update(status="error", error="timeout" if expired.is_set() else "sandbox_error",
                      detail=type(exc).__name__)
    finally:
        if timer:
            timer.cancel()
            timer.join(timeout=20)
        if expired.is_set():
            record.update(status="error", error="timeout")
        with guard:
            active.discard(request.session_id)
        record["duration_seconds"] = time.monotonic() - started
        journal.complete("op:" + request.operation_id, request.operation_id, record)
    return record


@app.get("/operations/{operation_id}/events")
def events(operation_id: str, after: int = 0):
    return {"version": "0", "events": journal.events(operation_id, after)}


@app.get("/sessions/{session_id}/files")
def collect(session_id: str):
    container = get_container(session_id)
    script = """import os,pathlib,json,base64,stat
files={}; total=0
for root,dirs,names in os.walk('/work',followlinks=False):
 dirs[:]=[d for d in dirs if not pathlib.Path(root,d).is_symlink()]
 for name in names:
  p=pathlib.Path(root,name)
  if p.is_symlink(): continue
  fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  try:
   info=os.fstat(fd)
   if not stat.S_ISREG(info.st_mode): continue
   if info.st_size>4*1024*1024 or total+info.st_size>8*1024*1024 or len(files)>=4096: raise SystemExit(3)
   with os.fdopen(os.dup(fd),'rb') as stream: data=stream.read(min(4*1024*1024,8*1024*1024-total)+1)
   if len(data)>4*1024*1024 or total+len(data)>8*1024*1024: raise SystemExit(3)
   total+=len(data)
   files[str(p.relative_to('/work'))]=base64.b64encode(data).decode()
  finally: os.close(fd)
print(json.dumps(files))
"""
    timer = threading.Timer(15, lambda: destroy(session_id))
    timer.start()
    try:
        result = container.exec_run(["/usr/local/bin/python", "-I", "-S", "-c", script], user="1000:1000")
    finally:
        timer.cancel()
    if result.exit_code == 3:
        raise HTTPException(413, "collection limit: 4 MiB per file, 8 MiB total, 4096 files")
    if result.exit_code != 0:
        raise HTTPException(500, "file collection failed")
    return {"version": "0", "files": json.loads(result.output)}


@app.get("/operations/{operation_id}/stream")
def stream(operation_id: str, request: Request, after: int = 0):
    journal.get("op:" + operation_id)
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except ValueError:
        raise HTTPException(422, "invalid event cursor")
    return StreamingResponse(stream_events(lambda value: journal.events(operation_id, value), cursor), media_type="text/event-stream")
