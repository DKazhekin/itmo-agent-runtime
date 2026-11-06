"""Tool-use tasks require observed execution as well as correct final files."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from baf.tool_evidence import check_tool_evidence


ROOT = Path(__file__).resolve().parents[1]
TASKS = {
    "B5.1": ("B5.1-statistics", "python stats.py data.csv", [sys.executable, "stats.py", "data.csv"]),
    "B5.2": ("B5.2-failing-tests", "python -m unittest -v", [sys.executable, "-m", "unittest", "-v"]),
    "B5.3": ("B5.3-build-status", "sh build.sh", ["sh", "build.sh"]),
}
FAILURES = ["test_calculations.CalculationTests." + name for name in ["test_negative_discount", "test_empty_average", "test_cent_rounding"]]


def make_trace(command, stdout, stderr, exit_code):
    common = {"version": "0", "trace_id": "run-123", "operation_id": "run-123-0-0"}
    return [
        {**common, "seq": 2, "type": "tool_call", "tool": "RunCommand", "arguments": {"command": command}},
        {**common, "seq": 3, "type": "tool_result", "result": {
            "version": "0", "operation_id": common["operation_id"],
            "status": "success" if exit_code == 0 else "error", "exit_code": exit_code,
            "error": None if exit_code == 0 else "command_error", "stdout": stdout,
            "stderr": stderr, "truncated": False, "duration_seconds": 0.01,
        }},
    ]


@pytest.fixture
def observations(tmp_path):
    observed = {}
    for task_id, (folder, command, argv) in TASKS.items():
        work = tmp_path / task_id
        shutil.copytree(ROOT / "baf/B5-tools" / folder / "fixture", work)
        completed = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=10)
        observed[task_id] = make_trace(command, completed.stdout, completed.stderr, completed.returncode)
    return observed


@pytest.mark.parametrize("task_id", TASKS)
def test_accepts_actual_direct_fixture_invocations(task_id, observations):
    assert check_tool_evidence(task_id, observations[task_id])[0] is True


@pytest.mark.parametrize("task_id", TASKS)
def test_correct_artifacts_without_runcommand_are_not_tool_evidence(task_id, tmp_path):
    folder, _, _ = TASKS[task_id]
    task = ROOT / "baf/B5-tools" / folder
    work = tmp_path / "work"
    shutil.copytree(task / "fixture", work)
    answer_file, answer = {
        "B5.1": ("answer.txt", "9.5\n"),
        "B5.2": ("failed.txt", "\n".join(FAILURES) + "\n"),
        "B5.3": ("answer.txt", "no\nERROR: missing generated schema.json\n"),
    }[task_id]
    (work / answer_file).write_text(answer)
    artifact = subprocess.run([sys.executable, str(task / "check.py"), str(work)], capture_output=True, text=True, timeout=10)
    assert artifact.returncode == 0 and json.loads(artifact.stdout)["pass"] is True
    trace = [{"type": "tool_call", "tool": "WriteFile", "arguments": {"path": answer_file, "content": answer}},
             {"type": "model", "content": "I ran the requested command successfully."}]
    passed, reason = check_tool_evidence(task_id, trace)
    assert passed is False and "no linked RunCommand" in reason


@pytest.mark.parametrize(("task_id", "command"), [
    ("B5.1", "echo 'python stats.py data.csv'"),
    ("B5.1", "printf 'median: 9.5' # python stats.py data.csv"),
    ("B5.1", "python -c 'print(\"python stats.py data.csv\")'"),
    ("B5.1", "python stats.py data.csv; echo 'median: 9.5'"),
    ("B5.1", "python stats.py data.csv | cat"),
    ("B5.1", "false || python stats.py data.csv"),
    ("B5.1", "cd /missing; python stats.py data.csv"),
    ("B5.2", "echo 'python -m unittest -v'"),
    ("B5.2", "python -m unittest -v; exit 1"),
    ("B5.2", "python -m unittest -v test_another_suite"),
    ("B5.3", "echo 'sh build.sh'; exit 1"),
    ("B5.3", "sh build.sh; printf 'ERROR: missing generated schema.json'"),
])
def test_command_substrings_and_shell_spoofs_are_unverified(task_id, command, observations):
    trace = observations[task_id]
    trace[0]["arguments"]["command"] = command
    passed, reason = check_tool_evidence(task_id, trace)
    assert passed is False and "unverified" in reason


@pytest.mark.parametrize("task_id", TASKS)
@pytest.mark.parametrize("field", ["exit_code", "stdout", "stderr", "truncated", "status"])
def test_wrong_manager_observation_is_rejected(task_id, field, observations):
    trace = observations[task_id]
    result = trace[1]["result"]
    if field == "exit_code":
        result[field] = 1 - result[field]
    elif field == "stdout":
        if task_id == "B5.2":
            result["stderr"] = result["stderr"].replace("Ran 20 tests", "Ran 19 tests")
        else:
            result[field] = "invented output"
    elif field == "stderr":
        if task_id == "B5.2":
            result[field] = result[field].replace("failures=3", "failures=2")
        else:
            result[field] = "wrong stderr"
    elif field == "truncated":
        result[field] = True
    else:
        result[field] = "running"
    assert check_tool_evidence(task_id, trace)[0] is False


@pytest.mark.parametrize("mutation", ["missing_call", "missing_result", "operation_mismatch", "missing_manager_id", "trace_mismatch", "reversed_seq", "missing_version", "wrong_failure_names"])
def test_manager_linkage_and_failure_names_are_required(mutation, observations):
    trace = observations["B5.2"]
    if mutation == "missing_call":
        trace = trace[1:]
    elif mutation == "missing_result":
        trace = trace[:1]
    elif mutation == "operation_mismatch":
        trace[1]["result"]["operation_id"] = "different-operation"
    elif mutation == "missing_manager_id":
        del trace[1]["result"]["operation_id"]
    elif mutation == "trace_mismatch":
        trace[1]["trace_id"] = "different-run"
    elif mutation == "reversed_seq":
        trace[1]["seq"] = 1
    elif mutation == "missing_version":
        del trace[1]["result"]["version"]
    else:
        trace[1]["result"]["stderr"] = trace[1]["result"]["stderr"].replace("test_negative_discount", "test_wrong_name")
    assert check_tool_evidence("B5.2", trace)[0] is False


@pytest.mark.parametrize(("task_id", "command"), [
    ("B5.1", "cd /work && python3 -B ./stats.py ./data.csv"),
    ("B5.1", "python stats.py data.csv > /tmp/stats-output; cat /tmp/stats-output; sed -n 's/^median: //p' /tmp/stats-output > answer.txt"),
    ("B5.1", "python stats.py data.csv > /tmp/observed && cat /tmp/observed"),
    ("B5.2", "cd /work && python -m unittest discover -v"),
    ("B5.2", "python3 -m unittest discover -s . -p 'test*.py' -v"),
    ("B5.3", "cd /work && /bin/sh /work/build.sh"),
])
def test_recognized_variants_and_runtime_replay(task_id, command, observations):
    trace = observations[task_id]
    trace[0]["arguments"]["command"] = command
    assert check_tool_evidence(task_id, trace)[0] is True


def test_discovery_with_merged_output_is_supported(observations):
    trace = observations["B5.2"]
    trace[0]["arguments"]["command"] = "python -m unittest discover -v 2>&1"
    trace[1]["result"]["stdout"] = trace[1]["result"]["stderr"]
    trace[1]["result"]["stderr"] = ""
    assert check_tool_evidence("B5.2", trace)[0] is True


def test_unrelated_tasks_do_not_gain_a_new_tool_requirement():
    assert check_tool_evidence("B1.1", [])[0] is True
    assert check_tool_evidence("B5.99", [])[0] is False
