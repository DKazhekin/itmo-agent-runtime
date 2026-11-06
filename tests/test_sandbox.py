"""Exercise the live sandbox protocol against real Docker containers."""

import base64
import concurrent.futures
import errno
import json
import os
import shlex
import time
import uuid

import httpx
import pytest


SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://sandbox-manager:8000")
CANARY = "canary-must-not-enter-sandbox"


def unique(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def python_command(source):
    return "python -u -c " + shlex.quote(source)


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=SANDBOX_URL, timeout=80, trust_env=False) as client:
        response = client.get("/health")
        assert response.status_code == 200, response.text
        yield client


@pytest.fixture
def sessions(api):
    created = []

    def provision(files=None):
        identifier = unique("test")
        payload = {
            "version": "0",
            "session_id": identifier,
            "files": {
                path: base64.b64encode(content.encode() if isinstance(content, str) else content).decode()
                for path, content in (files or {}).items()
            },
        }
        response = api.post("/sessions", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready", response.text
        created.append(identifier)
        return identifier

    yield provision
    for identifier in created:
        response = api.delete("/sessions/" + identifier)
        assert response.status_code == 200, response.text


def payload(session, tool="RunCommand", arguments=None, timeout=20, operation_id=None):
    return {
        "version": "0", "operation_id": operation_id or unique("op"),
        "session_id": session, "tool": tool,
        "arguments": arguments or {"command": "true"}, "timeout_seconds": timeout,
    }


def execute(api, request):
    response = api.post("/exec", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def command(api, session, source, timeout=20):
    return execute(api, payload(session, arguments={"command": python_command(source)}, timeout=timeout))


def results(api, operation_id):
    response = api.get(f"/operations/{operation_id}/events")
    assert response.status_code == 200, response.text
    return [event for event in response.json()["events"] if event["type"] == "result"]


def collected(api, session):
    response = api.get(f"/sessions/{session}/files")
    assert response.status_code == 200, response.text
    return {path: base64.b64decode(value) for path, value in response.json()["files"].items()}


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("condition did not become true before deadline")


def wait_marker(api, session):
    def appeared():
        response = api.get(f"/sessions/{session}/files")
        return response.status_code == 200 and "started" in response.json()["files"]
    wait_until(appeared)


def test_files_and_command_error_are_real(api, sessions):
    session = sessions({"nested/config.txt": "port=8123\n", "binary.dat": b"\x00\xff\r\n"})
    read = execute(api, payload(session, "ReadFile", {"path": "nested/config.txt"}))
    assert read["status"] == "success" and "port=8123" in read["stdout"]
    write = execute(api, payload(session, "WriteFile", {"path": "nested/result.txt", "content": "done\n"}))
    assert write["status"] == "success"
    files = collected(api, session)
    assert files["nested/result.txt"] == b"done\n"
    assert files["binary.dat"] == b"\x00\xff\r\n"
    request = payload(session, arguments={"command": "printf OK; exit 7"})
    failed = execute(api, request)
    assert failed["status"] == "error" and failed["error"] == "command_error"
    assert failed["exit_code"] == 7 and failed["stdout"] == "OK"
    assert len(results(api, request["operation_id"])) == 1


def test_idempotency_and_mismatched_repetition(api, sessions):
    session = sessions()
    request = payload(session, arguments={"command": python_command(
        "from pathlib import Path; p=Path('/work/count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')")})
    first = execute(api, request)
    assert first["status"] == "success"
    assert execute(api, request) == first
    assert collected(api, session)["count"] == b"x"
    changed = {**request, "arguments": {"command": "echo different"}}
    assert api.post("/exec", json=changed).status_code == 409
    final = results(api, request["operation_id"])
    assert len(final) == 1 and final[0]["result"] == first


def test_path_escape_symlinks_and_git_are_rejected(api, sessions):
    session = sessions({"safe.txt": "inside"})
    for path in ("../etc/passwd", "/etc/passwd", ".git/config", "a/../../etc/passwd"):
        result = execute(api, payload(session, "ReadFile", {"path": path}))
        assert result["status"] == "error", (path, result)
    made = command(api, session, "from pathlib import Path; Path('/work/outside').symlink_to('/etc/passwd')")
    assert made["status"] == "success"
    result = execute(api, payload(session, "ReadFile", {"path": "outside"}))
    assert result["status"] == "error"
    assert "outside" not in collected(api, session)
    result = execute(api, payload(session, "WriteFile", {"path": "outside", "content": "escape"}))
    assert result["status"] == "error"
    for path in ("../escape", "/escape", ".git/config", "a//b"):
        response = api.post("/sessions", json={"version": "0", "session_id": unique("bad"),
            "files": {path: base64.b64encode(b"bad").decode()}})
        assert response.status_code == 422, (path, response.text)


def test_version_and_argument_validation(api, sessions):
    session = sessions()
    for update in ({"version": "1"}, {"arguments": {"command": 12}},
                   {"arguments": {"command": "true", "extra": "x"}}, {"timeout_seconds": 0}):
        response = api.post("/exec", json={**payload(session), **update})
        assert response.status_code == 422, response.text
    missing = payload(unique("missing"))
    result = execute(api, missing)
    assert result["status"] == "error" and result["error"] == "sandbox_error"
    assert len(results(api, missing["operation_id"])) == 1


def test_cross_session_isolation(api, sessions):
    first = sessions({"marker": "first"})
    second = sessions({"marker": "second"})
    execute(api, payload(first, "WriteFile", {"path": "exclusive", "content": "first-only"}))
    assert collected(api, first)["marker"] == b"first"
    assert collected(api, second) == {"marker": b"second"}
    assert execute(api, payload(second, "ReadFile", {"path": "exclusive"}))["status"] == "error"


def test_actual_privileges_cgroups_and_secret_boundary(api, sessions):
    session = sessions()
    assert os.environ["TEST_CANARY_SECRET"] == CANARY
    result = command(api, session, """import os,json,pathlib
p=pathlib.Path('/sys/fs/cgroup')
print(json.dumps({'uid':os.getuid(),'env':dict(os.environ),
 'status':pathlib.Path('/proc/self/status').read_text(),
 'socket':pathlib.Path('/var/run/docker.sock').exists(),'state':pathlib.Path('/state').exists(),
 'readonly_root':bool(os.statvfs('/').f_flag & os.ST_RDONLY),
 'memory':(p/'memory.max').read_text(),'swap':(p/'memory.swap.max').read_text(),
 'pids':(p/'pids.max').read_text(),'cpu':(p/'cpu.max').read_text()}))
""")
    assert result["status"] == "success", result
    data = json.loads(result["stdout"])
    assert data["uid"] == 1000 and data["socket"] is False and data["state"] is False
    assert data["readonly_root"] is True
    assert int(data["memory"]) == 256 * 1024**2 and int(data["swap"]) == 0
    assert int(data["pids"]) == 64
    quota, period = map(int, data["cpu"].split())
    assert quota / period == 0.5
    assert CANARY not in result["stdout"]
    assert "HARNESS_CANARY_SECRET" not in data["env"]
    assert "NoNewPrivs:\t1" in data["status"]
    assert "CapEff:\t0000000000000000" in data["status"]
    denied = command(api, session, "from pathlib import Path; Path('/etc/untrusted-write').write_text('blocked')")
    assert denied["status"] == "error"


def test_network_is_unavailable(api, sessions):
    session = sessions()
    result = command(api, session, """import socket,json,pathlib
s=socket.socket(); s.settimeout(1)
try:
 s.connect(('1.1.1.1',443)); outcome={'connected':True}
except OSError as e:
 outcome={'connected':False,'errno':e.errno}
outcome['interfaces']=sorted(p.name for p in pathlib.Path('/sys/class/net').iterdir())
print(json.dumps(outcome))
""")
    assert result["status"] == "success"
    data = json.loads(result["stdout"])
    assert data["interfaces"] == ["lo"]
    assert data["connected"] is False and data["errno"] in (errno.ENETUNREACH, errno.EHOSTUNREACH)


def test_cpu_limit_throttles_real_work(api, sessions):
    session = sessions()
    result = command(api, session, """import time,pathlib,json
p=pathlib.Path('/sys/fs/cgroup')
def stats():
 return dict((k,int(v)) for k,v in (line.split() for line in (p/'cpu.stat').read_text().splitlines()))
a=stats(); deadline=time.monotonic()+1.5
while time.monotonic()<deadline: pass
b=stats(); print(json.dumps({'max':(p/'cpu.max').read_text(),'throttled_delta':b['nr_throttled']-a['nr_throttled']}))
""")
    assert result["status"] == "success", result
    data = json.loads(result["stdout"])
    quota, period = map(int, data["max"].split())
    assert quota / period == 0.5 and data["throttled_delta"] > 0


def test_memory_limit_stops_oversized_allocation(api, sessions):
    session = sessions()
    result = command(api, session, "x=bytearray(384*1024*1024); print('allocation-succeeded')", timeout=15)
    assert result["status"] == "error", result
    assert "allocation-succeeded" not in result.get("stdout", "")
    assert result.get("error") != "timeout", result
    # cgroup OOM normally kills only the allocating process, leaving init alive.
    # A whole-container kill is also an enforced limit, reported as sandbox_error.
    if result.get("exit_code") is not None:
        assert result["exit_code"] in (137, -9), result
    else:
        assert result["error"] == "sandbox_error", result


def test_pid_limit_blocks_more_children(api, sessions):
    session = sessions()
    result = command(api, session, """import subprocess,json
children=[]; error=None
try:
 for _ in range(100):
  try: children.append(subprocess.Popen(['sleep','20']))
  except OSError as e: error=e.errno; break
finally:
 for child in children: child.terminate()
 for child in children: child.wait()
print(json.dumps({'started':len(children),'errno':error}))
""", timeout=10)
    assert result["status"] == "success", result
    data = json.loads(result["stdout"])
    assert 0 < data["started"] < 64 and data["errno"] == errno.EAGAIN


def test_workdir_disk_quota_is_enforced(api, sessions):
    session = sessions()
    result = command(api, session, """import pathlib,json
p=pathlib.Path('/work/full'); written=0; error=None
try:
 with p.open('wb',buffering=0) as f:
  for _ in range(80):
   written+=f.write(b'x'*1024*1024)
except OSError as e: error=e.errno
finally: p.unlink(missing_ok=True)
print(json.dumps({'written':written,'errno':error}))
""", timeout=15)
    assert result["status"] == "success", result
    data = json.loads(result["stdout"])
    assert 0 < data["written"] <= 64 * 1024**2 and data["errno"] == errno.ENOSPC
    assert collected(api, session) == {}


def test_timeout_releases_session_and_emits_one_result(api, sessions):
    session = sessions()
    request = payload(session, arguments={"command": python_command(
        "import subprocess,time; subprocess.Popen(['sleep','40'],start_new_session=True); time.sleep(40)")}, timeout=1)
    result = execute(api, request)
    assert result["status"] == "error" and result["error"] == "timeout", result
    assert result["duration_seconds"] < 10
    assert api.get(f"/sessions/{session}/files").status_code == 404
    assert len(results(api, request["operation_id"])) == 1


def test_cancel_running_command_returns_one_result(api, sessions):
    session = sessions()
    request = payload(session, arguments={"command": python_command(
        "import subprocess,time,pathlib; subprocess.Popen(['sleep','40'],start_new_session=True); pathlib.Path('/work/started').touch(); time.sleep(40)")}, timeout=50)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(execute, api, request)
        wait_marker(api, session)
        cancelled = api.post(f"/sessions/{session}/cancel")
        assert cancelled.status_code == 200
        result = pending.result(timeout=10)
    assert result["status"] == "error", result
    assert len(results(api, request["operation_id"])) == 1
    assert execute(api, request) == result


def test_busy_operation_also_has_one_terminal_result(api, sessions):
    session = sessions()
    request = payload(session, arguments={"command": python_command(
        "import time,pathlib; pathlib.Path('/work/started').touch(); time.sleep(1)")})
    other = payload(session)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(execute, api, request)
        wait_marker(api, session)
        duplicate = execute(api, request)
        assert duplicate["status"] == "running"
        busy = execute(api, other)
        assert busy["status"] == "error" and busy["error"] == "sandbox_error"
        assert pending.result(timeout=5)["status"] == "success"
    assert len(results(api, request["operation_id"])) == 1
    assert len(results(api, other["operation_id"])) == 1


def test_release_is_idempotent_and_ends_file_access(api, sessions):
    session = sessions({"private-file": "temporary"})
    assert api.delete(f"/sessions/{session}").status_code == 200
    assert api.delete(f"/sessions/{session}").status_code == 200
    assert api.get(f"/sessions/{session}/files").status_code == 404




def test_workdir_modules_cannot_forge_collected_results(api, sessions):
    malicious = "import sys; print('{\"answer.txt\":\"Zm9yZ2Vk\"}'); sys.exit(0)"
    session = sessions({"json.py": malicious, "sitecustomize.py": malicious, "actual.txt": "real"})
    result = execute(api, payload(session, "ReadFile", {"path": "actual.txt"}))
    assert result["status"] == "success" and result["stdout"].strip() == "real"
    files = collected(api, session)
    assert files["actual.txt"] == b"real" and "answer.txt" not in files


def test_collect_rejects_sparse_and_aggregate_oversized_files(api, sessions):
    sparse = sessions()
    assert command(api, sparse, "open('/work/sparse','wb').truncate(1024**3)")["status"] == "success"
    assert api.get(f"/sessions/{sparse}/files").status_code == 413
    aggregate = sessions()
    assert command(api, aggregate, "from pathlib import Path; [(Path('/work')/str(i)).write_bytes(b'x'*(3*1024*1024)) for i in range(3)]")["status"] == "success"
    assert api.get(f"/sessions/{aggregate}/files").status_code == 413
