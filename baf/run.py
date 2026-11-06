"""Run tasks and preserve every attempt, including errors; never treat replay as quality."""
import argparse
import datetime
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

from client.cli import Client
from baf.tool_evidence import check_tool_evidence


def run_task(task_path, root, client, mode="real", expected_model=None):
    task = yaml.safe_load(task_path.read_text())
    task_id = task.get("id", task_path.parent.name.split("-")[0])
    output = root / task_id
    output.mkdir(parents=True)
    started = time.monotonic()
    result = {"task": task_id, "pass": False}
    trace = []
    try:
        instruction = task.get("instruction") or "\n".join((m if isinstance(m, str) else m["content"]) for m in task["messages"])
        if mode == "mock":
            instruction += f"\n[replay:{task_id}]"
        def preserve(event):
            trace.append(event)
            with (output / "trace.jsonl").open("a") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        state, trace = client.execute(task_path.parent / "fixture", instruction, output / "work",
            timeout=task.get("timeout_seconds", task.get("timeout_s", 240)), on_event=preserve,
            expected_mode=mode, expected_model=expected_model)
        result["state"] = state
        if state["status"] == "success":
            if any(p.is_symlink() for p in (output / "work").rglob("*")):
                raise ValueError("symlink in collected work")
            checked = subprocess.run([sys.executable, str(task_path.parent / "check.py"), str(output / "work")],
                capture_output=True, text=True, timeout=30)
            result["checker"] = {"exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr}
            verdict = json.loads(checked.stdout)
            evidence_ok, evidence_reason = check_tool_evidence(task_id, trace)
            result["tool_evidence"] = {"pass": evidence_ok, "reason": evidence_reason}
            result["pass"] = checked.returncode == 0 and verdict.get("pass") is True and evidence_ok
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:1000]
    finally:
        if trace:
            (output / "trace.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in trace))
        result["duration_seconds"] = time.monotonic() - started
    model_events = [event for event in trace if event.get("type") == "model"]
    usages = [event.get("usage", {}) for event in model_events]
    replay_only = bool(model_events) and all(event.get("mode") == "mock" for event in model_events)
    tokens_complete = replay_only or (
        bool(usages) and not result.get("error") and result.get("state", {}).get("status") == "success"
        and all(isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens")) for usage in usages)
    )
    result.update(task_id=task_id, repeat=1, seconds=result["duration_seconds"],
                  tokens_in=0 if replay_only else sum(x["prompt_tokens"] for x in usages) if tokens_complete else None,
                  tokens_out=0 if replay_only else sum(x["completion_tokens"] for x in usages) if tokens_complete else None,
                  tokens_complete=tokens_complete,
                  outcome=result.get("state", {}).get("status", "error"),
                  models=sorted({e["model"] for e in trace if e.get("model")}),
                  modes=sorted({e["mode"] for e in trace if e.get("mode")}))
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def validate_evidence(evidence):
    """Validate recorded approval metadata; this is not spending accounting or enforcement."""
    if not isinstance(evidence, dict):
        raise ValueError("model evidence must be an object")
    for field in ("model", "version", "index_date", "access", "approval"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(f"model evidence requires nonempty {field}")
    try:
        date = datetime.date.fromisoformat(evidence["index_date"])
        if date.isoformat() != evidence["index_date"]:
            raise ValueError()
    except ValueError:
        raise ValueError("model evidence index_date must use YYYY-MM-DD") from None
    for field in ("index", "budget_limit"):
        value = evidence.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"model evidence {field} must be a finite nonnegative number")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "real"], required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("results/baf"))
    parser.add_argument("--model-evidence", type=Path)
    args = parser.parse_args()
    if args.mode == "real":
        if not args.model_evidence:
            parser.error("real runs require --model-evidence with model/version/index/date/access/budget")
        try:
            evidence = json.loads(args.model_evidence.read_text())
            validate_evidence(evidence)
        except (OSError, ValueError) as exc:
            parser.error(f"{exc}; the evidence file records approval, not spending accounting or enforcement")
    else:
        evidence = {"mode": "mock", "quality_evidence": False}
    tasks = sorted(Path("baf").glob("B[1-5]-*/B*/task.yaml"))
    if args.mode == "mock" and not args.task:
        tasks = [t for t in tasks if t.parent.name.split("-")[0] in ("B1.1", "B2.1", "B3.1", "B4.1", "B5.1")]
    if args.task:
        tasks = [t for t in tasks if t.parent.name.split("-")[0] in args.task]
    if not tasks:
        parser.error("no matching tasks")
    if args.mode == "real" and not args.task and len(tasks) != 15:
        parser.error("expected all 15 B1-B5 tasks")
    attempt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    root = args.output / args.mode / attempt
    root.mkdir(parents=True)
    manifest = {"attempt": attempt, "mode": args.mode, "model_evidence": evidence,
        "course_commit": "b01ddf7b89e5c603985e3d5b5af958bebc7b1972",
        "tasks": [str(t) for t in tasks], "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("runtime", "baf", "client") for p in Path(folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    client = Client()
    expected_model = evidence.get("model") if args.mode == "real" else None
    results = []
    stopped_reason = None
    try:
        client.preflight(expected_mode=args.mode, expected_model=expected_model)
    except Exception as exc:
        stopped_reason = "preflight failed: " + type(exc).__name__ + ": " + str(exc)[:1000]
    if stopped_reason is None:
        for task in tasks:
            result = run_task(task, root, client, args.mode, expected_model=expected_model)
            mode = result.get("state", {}).get("model_mode")
            if (mode is not None and mode != args.mode) or result.get("modes") != [args.mode]:
                result["pass"] = False
                result["mode_error"] = "runtime mode differs from requested run mode or is unconfirmed"
            if args.mode == "real" and result.get("models") != [evidence["model"]]:
                result["pass"] = False
                result["mode_error"] = "observed model differs from approved evidence or is unconfirmed"
            tool_evidence = result.get("tool_evidence", {})
            evidence_failure = tool_evidence.get("reason") if tool_evidence.get("pass") is False else None
            state = result.get("state", {})
            state_failure = state.get("status") in ("error", "cancelled")
            state_reason = (state.get("error", state.get("status", "error")) + ": " + state.get("detail", "")).rstrip(": ") if state_failure else None
            result["reason"] = (result.get("error") or state_reason or result.get("mode_error") or evidence_failure
                                or result.get("checker", {}).get("stdout", "no checker"))
            (root / result["task"] / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            results.append(result)
            if result.get("mode_error") or state_failure:
                stopped_reason = result["reason"]
                break
    (root / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    confirmed_real = bool(results) and args.mode == "real" and all(
        r.get("modes") == ["real"] and r.get("models") == [evidence["model"]] for r in results)
    summary = {"passed": sum(r["pass"] for r in results), "total": len(results),
               "requested_total": len(tasks), "not_run": [str(task) for task in tasks[len(results):]],
               "quality_evidence": confirmed_real, "results": results}
    if stopped_reason:
        summary["stopped_reason"] = stopped_reason
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"path": str(root), "passed": summary["passed"], "total": len(results),
                      "requested_total": len(tasks), "stopped_reason": stopped_reason}))
    raise SystemExit(bool(stopped_reason) or summary["passed"] != len(tasks))



if __name__ == "__main__":
    main()
