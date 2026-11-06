"""Verify run approval metadata, early mode checks, and honest usage reports offline."""
import json
import sys
from pathlib import Path

import pytest

from baf import run


@pytest.fixture
def evidence():
    return {"model": "approved-model", "version": "2026-11", "index": 50,
            "index_date": "2026-11-06", "access": "authorized test endpoint",
            "budget_limit": 0, "approval": "synthetic test approval, no paid requests"}


@pytest.mark.parametrize("field", ["model", "version", "index_date", "access", "approval"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_approval_metadata_is_rejected(evidence, field, value):
    evidence[field] = value
    with pytest.raises(ValueError):
        run.validate_evidence(evidence)


@pytest.mark.parametrize("field", ["index", "budget_limit"])
@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "10", None])
def test_invalid_numeric_evidence_is_rejected(evidence, field, value):
    evidence[field] = value
    with pytest.raises(ValueError):
        run.validate_evidence(evidence)


def test_valid_evidence_accepts_zero_budget_but_requires_iso_date(evidence):
    run.validate_evidence(evidence)
    evidence["index_date"] = "20261106"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        run.validate_evidence(evidence)


def fixture_task(tmp_path):
    task = tmp_path / "task"
    (task / "fixture").mkdir(parents=True)
    (task / "task.yaml").write_text("id: B1.1\ninstruction: Read the port\n")
    (task / "check.py").write_text('import json\nprint(json.dumps({"pass": True}))\n')
    return task / "task.yaml"


class RecordedClient:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.expectations = None

    def execute(self, fixture, instruction, destination, **kwargs):
        self.expectations = (kwargs["expected_mode"], kwargs["expected_model"])
        for event in self.events:
            kwargs["on_event"](event)
        if self.failure:
            raise self.failure
        destination.mkdir(parents=True)
        return {"status": "success", "model_mode": "real"}, self.events


def model_event(usage, mode="real"):
    return {"type": "model", "mode": mode, "model": "approved-model" if mode == "real" else None,
            "usage": usage}


@pytest.mark.parametrize("usage", [{}, None, {"prompt_tokens": 10},
                                   {"prompt_tokens": True, "completion_tokens": 2}])
def test_unknown_usage_is_not_zero(tmp_path, usage):
    client = RecordedClient([model_event(usage)])
    result = run.run_task(fixture_task(tmp_path), tmp_path / "results", client,
                          mode="real", expected_model="approved-model")
    assert result["tokens_in"] is None and result["tokens_out"] is None
    assert result["tokens_complete"] is False
    assert client.expectations == ("real", "approved-model")
    assert json.loads((tmp_path / "results/B1.1/result.json").read_text()) == result


def test_usage_is_summed_only_when_complete(tmp_path):
    client = RecordedClient([model_event({"prompt_tokens": 10, "completion_tokens": 2}),
                             model_event({"prompt_tokens": 20, "completion_tokens": 3})])
    result = run.run_task(fixture_task(tmp_path), tmp_path / "results", client)
    assert (result["tokens_in"], result["tokens_out"], result["tokens_complete"]) == (30, 5, True)


def test_missing_model_events_do_not_claim_zero_usage(tmp_path):
    result = run.run_task(fixture_task(tmp_path), tmp_path / "results", RecordedClient([]))
    assert (result["tokens_in"], result["tokens_out"], result["tokens_complete"]) == (None, None, False)


def test_mock_events_have_known_zero_provider_tokens(tmp_path):
    result = run.run_task(fixture_task(tmp_path), tmp_path / "results",
                          RecordedClient([model_event({}, "mock")]), mode="mock")
    assert (result["tokens_in"], result["tokens_out"], result["tokens_complete"]) == (0, 0, True)


def test_client_failure_retains_events_and_does_not_hide_possible_charge(tmp_path):
    events = [model_event({"prompt_tokens": 10, "completion_tokens": 3})]
    result = run.run_task(fixture_task(tmp_path), tmp_path / "results",
                          RecordedClient(events, TimeoutError("lost connection")))
    assert result["pass"] is False
    assert result["models"] == ["approved-model"] and result["modes"] == ["real"]
    assert result["tokens_complete"] is False and result["tokens_in"] is None
    assert [json.loads(line) for line in (tmp_path / "results/B1.1/trace.jsonl").read_text().splitlines()] == events
    assert json.loads((tmp_path / "results/B1.1/result.json").read_text())["error"] == "TimeoutError: lost connection"


def prepare_main(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for task_id in ("B1.1", "B1.2"):
        folder = tmp_path / "baf/B1-read" / (task_id + "-task")
        folder.mkdir(parents=True)
        (folder / "task.yaml").write_text("instruction: Read the port\n")
    monkeypatch.setattr(sys, "argv", ["baf.run", "--mode", "mock", "--task", "B1.1", "--task", "B1.2"])


def test_main_preflight_failure_prevents_all_tasks_and_preserves_attempt(tmp_path, monkeypatch):
    prepare_main(tmp_path, monkeypatch)
    class WrongRuntime:
        def preflight(self, **kwargs):
            assert kwargs == {"expected_mode": "mock", "expected_model": None}
            raise ValueError("runtime mode differs")
    monkeypatch.setattr(run, "Client", WrongRuntime)
    monkeypatch.setattr(run, "run_task", lambda *a, **k: pytest.fail("task must not start"))
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1
    attempt = next((tmp_path / "results/baf/mock").iterdir())
    assert (attempt / "manifest.json").is_file()
    summary = json.loads((attempt / "summary.json").read_text())
    assert summary["total"] == 0 and summary["requested_total"] == 2
    assert summary["quality_evidence"] is False and len(summary["not_run"]) == 2
    assert "runtime mode differs" in summary["stopped_reason"]


def test_main_stops_after_first_observed_mode_mismatch(tmp_path, monkeypatch):
    prepare_main(tmp_path, monkeypatch)
    class Ready:
        def preflight(self, **kwargs):
            return {"model_mode": "mock", "model": None}
    monkeypatch.setattr(run, "Client", Ready)
    observed = []
    def task(path, root, client, mode, expected_model):
        observed.append(path)
        (root / "B1.1").mkdir()
        return {"task": "B1.1", "pass": True, "state": {"model_mode": "real"},
                "modes": ["real"], "models": ["unexpected-model"]}
    monkeypatch.setattr(run, "run_task", task)
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1 and len(observed) == 1
    summary = json.loads(next((tmp_path / "results/baf/mock").glob("*/summary.json")).read_text())
    assert summary["passed"] == 0 and summary["total"] == 1 and len(summary["not_run"]) == 1


def test_bad_evidence_fails_before_constructing_client(tmp_path, monkeypatch, evidence):
    evidence["budget_limit"] = -1
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    monkeypatch.setattr(sys, "argv", ["baf.run", "--mode", "real", "--model-evidence", str(path)])
    monkeypatch.setattr(run, "Client", lambda: pytest.fail("client must not be constructed"))
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 2


def test_failed_tool_evidence_is_reported_even_when_file_checker_passes(tmp_path, monkeypatch):
    prepare_main(tmp_path, monkeypatch)
    class Ready:
        def preflight(self, **kwargs):
            return {"model_mode": "mock", "model": None}
    monkeypatch.setattr(run, "Client", Ready)
    def task(path, root, client, mode, expected_model):
        task_id = path.parent.name.split("-")[0]
        (root / task_id).mkdir()
        return {"task": task_id, "pass": False, "state": {"model_mode": "mock"},
                "modes": ["mock"], "models": [], "checker": {"stdout": '{"pass": true}'},
                "tool_evidence": {"pass": False, "reason": "missing observed command execution"}}
    monkeypatch.setattr(run, "run_task", task)
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1
    summary = json.loads(next((tmp_path / "results/baf/mock").glob("*/summary.json")).read_text())
    assert summary["total"] == 2
    assert all(result["reason"] == "missing observed command execution" for result in summary["results"])


@pytest.mark.parametrize("mode", ["mock", "real"])
def test_main_preserves_confirmed_model_failure_instead_of_mode_mismatch(tmp_path, monkeypatch, evidence, mode):
    prepare_main(tmp_path, monkeypatch)
    if mode == "real":
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(evidence))
        monkeypatch.setattr(sys, "argv", ["baf.run", "--mode", "real", "--model-evidence", str(path), "--task", "B1.1", "--task", "B1.2"])
    class Ready:
        def preflight(self, **kwargs):
            return {"model_mode": mode, "model": evidence["model"] if mode == "real" else None}
    monkeypatch.setattr(run, "Client", Ready)
    def task(path, root, client, mode, expected_model):
        (root / "B1.1").mkdir()
        return {"task": "B1.1", "pass": False,
                "state": {"status": "error", "error": "model_error", "detail": "incomplete_response"},
                "modes": [mode], "models": [expected_model] if expected_model else []}
    monkeypatch.setattr(run, "run_task", task)
    with pytest.raises(SystemExit) as error:
        run.main()
    assert error.value.code == 1
    summary = json.loads(next((tmp_path / "results/baf" / mode).glob("*/summary.json")).read_text())
    assert summary["stopped_reason"] == "model_error: incomplete_response"
    assert summary["total"] == 1 and len(summary["not_run"]) == 1
    assert "mode_error" not in summary["results"][0]
