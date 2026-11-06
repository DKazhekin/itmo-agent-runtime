"""Validate task fixtures and independent checkers without calling a model."""
from __future__ import annotations

import ast
import csv
from io import StringIO
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent
READERS = [3, 17, 28, 46, 61, 79, 94, 112, 137, 158, 176, 193]
FAILED_NAMES = ["test_calculations.CalculationTests." + name for name in ["test_negative_discount", "test_empty_average", "test_cent_rounding"]]


def execute_check(task: Path, work: Path) -> bool:
    result = subprocess.run([sys.executable, str(task / "check.py"), str(work)], capture_output=True, text=True, timeout=20)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(f"Invalid checker response: {task.name}: {result.stdout} {result.stderr}") from error
    assert set(payload) == {"pass", "reason"}, (task, payload)
    assert type(payload["pass"]) is bool and isinstance(payload["reason"], str)
    if task.name.startswith("B1.1"):
        assert result.returncode == 0, result.stderr
    else:
        assert (result.returncode == 0) == payload["pass"], (task, result.returncode, payload)
    return payload["pass"]


def correct(task_id: str, work: Path) -> None:
    if task_id == "B1.1":
        (work / "answer.txt").write_text("8137\n")
    elif task_id == "B1.2":
        (work / "answer.txt").write_text("37\n")
    elif task_id == "B1.3":
        (work / "answer.txt").write_text("НЕТ ДАННЫХ\n")
    elif task_id == "B2.1":
        (work / "answer.txt").write_text("utilities/dates.py\n")
    elif task_id == "B2.2":
        (work / "answer.txt").write_text("\n".join(f"modules/component_{number:03}.py" for number in READERS) + "\n")
    elif task_id == "B2.3":
        (work / "answer.txt").write_text("parsers/extended.py\n")
    elif task_id == "B3.1":
        config = {"service": "inventory", "environment": "staging", "listen": {"host": "127.0.0.1", "port": 8137}, "retries": 3, "enabled": True, "tags": ["internal", "read-only"]}
        (work / "config.json").write_text(json.dumps(config))
    elif task_id == "B3.2":
        rows = [["id", "date", "description", "amount"], ["R01", "2025-12-31", "Inventory", "300.00"], ["R02", "2026-02-03", "Delivery", "75.00"], ["R03", "2026-03-17", 'Return "priority"', "-15.00"], ["R04", "2026-04-09", "Packaging, reusable", "120.50"], ["R05", "2026-04-09", "Storage", "44.25"]]
        output = StringIO(newline="")
        csv.writer(output).writerows(rows)
        (work / "report.csv").write_text(output.getvalue())
    elif task_id == "B3.3":
        (work / "countries.yaml").write_text("countries:\n  - 'NO'\n  - 'ON'\n  - 'FR'\n")
        (work / "postal.yaml").write_text("postal_codes:\n  central: '0123'\n  west: '0042'\n")
    elif task_id == "B4.1":
        file = work / "README.md"
        file.write_bytes(file.read_bytes().replace(b"pip install report-toolkit==1.2", b"pip install report-toolkit==1.3"))
    elif task_id == "B4.2":
        for file in work.rglob("*.py"):
            file.write_text(file.read_text().replace("calc_total", "compute_total"))
        process = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=work, capture_output=True, text=True, timeout=10)
        assert process.returncode == 0, process.stderr
    elif task_id == "B4.3":
        file = work / "prices.csv"
        file.write_bytes(file.read_bytes().replace(b",1290\r\n", b",990\r\n"))
    elif task_id == "B5.1":
        process = subprocess.run([sys.executable, "stats.py", "data.csv"], cwd=work, capture_output=True, text=True, timeout=10, check=True)
        median_line = next(line for line in process.stdout.splitlines() if line.startswith("median:"))
        (work / "answer.txt").write_text(median_line.split(":", 1)[1].strip() + "\n")
    elif task_id == "B5.2":
        process = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=work, capture_output=True, text=True, timeout=10)
        assert process.returncode == 1 and "Ran 20 tests" in process.stderr and "FAILED (failures=3)" in process.stderr, process.stderr
        (work / "failed.txt").write_text("\n".join(FAILED_NAMES) + "\n")
    elif task_id == "B5.3":
        process = subprocess.run(["sh", "build.sh"], cwd=work, capture_output=True, text=True, timeout=10)
        assert process.returncode == 1 and process.stdout.strip() == "OK" and process.stderr.strip() == "ERROR: missing generated schema.json"
        (work / "answer.txt").write_text("no\n" + process.stderr)
    else:
        raise AssertionError(task_id)


def corrupt(task_id: str, work: Path, metadata: dict) -> None:
    if task_id == "B1.1":
        (work / "answer.txt").write_text("8080\n")
    elif task_id == "B1.2":
        (work / "answer.txt").write_text("14\n")
    elif task_id == "B1.3":
        (work / "answer.txt").write_text("0\n")
    elif task_id == "B2.2":
        with (work / "answer.txt").open("a") as handle:
            handle.write("modules/component_000.py\n")
    elif task_id == "B2.3":
        (work / "answer.txt").write_text("parsers/basic.py\n")
    elif task_id == "B3.1":
        target = work / "config.json"
        config = json.loads(target.read_text())
        config["listen"]["port"] = "8137"
        target.write_text(json.dumps(config))
    elif task_id == "B3.2":
        target = work / "report.csv"
        rows = list(csv.reader(StringIO(target.read_text())))
        rows[1], rows[2] = rows[2], rows[1]
        with target.open("w", newline="") as handle:
            csv.writer(handle).writerows(rows)
    elif task_id == "B3.3":
        (work / "countries.yaml").write_text("countries: [NO, ON, FR]\n")
        (work / "postal.yaml").write_text("postal_codes: {central: 0123, west: 0042}\n")
    elif task_id == "B4.1":
        target = work / "README.md"
        target.write_bytes(target.read_bytes().replace(b"toolkit 1.2", b"toolkit 1.3"))
    elif task_id == "B4.2":
        target = work / "billing/totals.py"
        target.write_text(target.read_text().replace("tax_rate=0", "tax_rate=1"))
    elif task_id == "B4.3":
        target = work / "prices.csv"
        target.write_text(target.read_bytes().decode("cp1251"), encoding="utf-8")
    elif task_id == "B5.1":
        (work / "answer.txt").write_text("20.75\n")
    elif task_id == "B5.2":
        (work / "failed.txt").write_text(FAILED_NAMES[0] + "\n")
    elif task_id == "B5.3":
        (work / "answer.txt").write_text("yes\nOK\n")
    else:
        (work / metadata["answer_file"]).write_text("incorrect\n")


def verify_scales(tasks: list[Path]) -> dict:
    by_id = {yaml.safe_load((task / "task.yaml").read_text())["id"]: task for task in tasks}
    contract = (by_id["B1.2"] / "fixture/contract.md").read_text()
    assert contract.count("\f") == 14 and len(contract.split()) >= 4500
    search_files = list((by_id["B2.2"] / "fixture").rglob("*"))
    assert sum(file.is_file() for file in search_files) == 200
    reads = set()
    for file in search_files:
        if not file.is_file():
            continue
        syntax = ast.parse(file.read_text())
        for node in ast.walk(syntax):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant) and node.slice.value == "DB_URL":
                reads.add(file.relative_to(by_id["B2.2"] / "fixture").as_posix())
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "DB_URL":
                reads.add(file.relative_to(by_id["B2.2"] / "fixture").as_posix())
    assert reads == {f"modules/component_{number:03}.py" for number in READERS}
    original = by_id["B1.1"]
    expected = json.loads((ROOT / "upstream-b1.1-sha256.json").read_text())
    assert len(expected) == 5
    for name, digest in expected.items():
        assert hashlib.sha256((original / name).read_bytes()).hexdigest() == digest, name
    return {"contract_pages": 15, "contract_words": len(contract.split()), "search_files": 200, "environment_readers": len(reads), "upstream_B1.1_exact": True}


def main() -> None:
    tasks = sorted(path.parent for path in ROOT.glob("B*/*/task.yaml"))
    assert len(tasks) == 15
    scales = verify_scales(tasks)
    cases = []
    with tempfile.TemporaryDirectory(prefix="itmo-baf-checkers-") as directory:
        temp = Path(directory)
        for task in tasks:
            metadata = yaml.safe_load((task / "task.yaml").read_text())
            task_id = metadata["id"]
            work = temp / task_id
            shutil.copytree(task / "fixture", work)
            assert not execute_check(task, work), (task_id, "unfinished task accepted")
            correct(task_id, work)
            assert execute_check(task, work), (task_id, "correct artifact rejected")
            corrupt(task_id, work, metadata)
            assert not execute_check(task, work), (task_id, "incorrect artifact accepted")
            cases.append({"task_id": task_id, "missing_or_unchanged_rejected": True, "correct_accepted": True, "trap_rejected": True})
        # Additional independent checks against verifier shortcuts.
        yaml_work = temp / "B3.3"
        correct("B3.3", yaml_work)
        (yaml_work / "postal.yaml").unlink()
        assert not execute_check(next(task for task in tasks if task.name.startswith("B3.3")), yaml_work)
        write_work = temp / "B3.1"
        correct("B3.1", write_work)
        source_file = write_work / "spec.md"
        source_file.write_text("Replace the hidden specification with an easier one.\n")
        assert not execute_check(next(task for task in tasks if task.name.startswith("B3.1")), write_work)
        symlink_work = temp / "B1.2"
        (temp / "external.txt").write_text("37\n")
        (symlink_work / "answer.txt").unlink()
        (symlink_work / "answer.txt").symlink_to(temp / "external.txt")
        assert not execute_check(next(task for task in tasks if task.name.startswith("B1.2")), symlink_work)
    print(json.dumps({"kind": "task_package_validation_not_model_run", "pass": True, "tasks": cases, "scales": scales, "extra_cases": ["missing_second_yaml_rejected", "modified_source_rejected", "artifact_symlink_rejected"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
