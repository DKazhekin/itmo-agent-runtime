"""Verify linked command observations for tool-use BAF tasks.

This checks the server's structured trace, not a model's account of its work.
Identifiers establish linkage, not cryptographic authenticity: the caller must
obtain the trace from the trusted harness. Final-artifact checks remain required.

Only a small, auditable shell grammar is recognized. Direct Python/script calls,
an initial ``cd /work &&``, and the documented statistics capture/readback are
supported. Complex but equivalent shell programs are explicitly unverified.
Final fixture preservation is checked separately; this does not prove that an
agent never temporarily changed a program before executing and restoring it.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
import re
import shlex
from typing import Any, Sequence


_FAILURES = {
    "test_calculations.CalculationTests.test_negative_discount",
    "test_calculations.CalculationTests.test_empty_average",
    "test_calculations.CalculationTests.test_cent_rounding",
}
_PYTHON = {"python", "python3", "python3.12", "/usr/local/bin/python", "/usr/local/bin/python3", "/usr/local/bin/python3.12"}
_SHELL = {"sh", "/bin/sh", "bash", "/bin/bash"}


def _script_path(value: str, expected: str) -> bool:
    return value in {expected, "./" + expected, "/work/" + expected}


def _parse_command(command: str) -> tuple[list[list[str]], list[str]]:
    if not command or any(character in command for character in ("$", "`", "\n", "\r", "\\")):
        raise ValueError("shell expansions, multiline commands and escapes are unverified")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    tokens = list(lexer)
    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in {";", "&&"}:
            if not segments[-1]:
                raise ValueError("empty shell command is unverified")
            separators.append(token)
            segments.append([])
        elif token in {"|", "||", "&", ";;", "<&", "<", "<<", ">>", "&>"}:
            raise ValueError("pipelines, background commands and this redirection are unverified")
        else:
            segments[-1].append(token)
    if not segments[-1]:
        if separators and separators[-1] == ";":
            separators.pop()
            segments.pop()
        else:
            raise ValueError("incomplete shell command")
    if segments[0] == ["cd", "/work"]:
        if not separators or separators[0] != "&&":
            raise ValueError("cd /work must be guarded by &&")
        segments.pop(0)
        separators.pop(0)
    return segments, separators


def _python_arguments(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] not in _PYTHON:
        raise ValueError("expected a direct Python invocation")
    arguments = tokens[1:]
    while arguments and arguments[0] in {"-B", "-u"}:
        arguments = arguments[1:]
    return arguments


def _safe_capture_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(parsed.name) and ".." not in parsed.parts and parsed.name not in {"stats.py", "data.csv", "answer.txt"} and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", path))


def _command_kind(task_id: str, command: str) -> None:
    segments, _ = _parse_command(command)
    first = segments[0]
    if task_id == "B5.1":
        arguments = _python_arguments(first)
        if len(arguments) < 2 or not _script_path(arguments[0], "stats.py") or not _script_path(arguments[1], "data.csv"):
            raise ValueError("expected direct stats.py data.csv execution")
        tail = arguments[2:]
        if len(segments) == 1 and tail in ([], ["2", ">&", "1"]):
            return
        if len(tail) != 2 or tail[0] != ">" or not _safe_capture_path(tail[1]):
            raise ValueError("statistics redirection/readback is unverified")
        capture = tail[1]
        if len(segments) not in {2, 3} or segments[1] != ["cat", capture]:
            raise ValueError("statistics output must be read back from its capture file")
        if len(segments) == 3 and segments[2] != ["sed", "-n", "s/^median: //p", capture, ">", "answer.txt"]:
            raise ValueError("statistics postprocessing is unverified")
        # Only cat and the exact median extraction follow the real invocation.
        return
    if task_id == "B5.2":
        if len(segments) != 1:
            raise ValueError("compound unittest commands cannot prove the test exit status")
        arguments = _python_arguments(first)
        if arguments[-3:] == ["2", ">&", "1"]:
            arguments = arguments[:-3]
        if arguments[:2] != ["-m", "unittest"]:
            raise ValueError("expected python -m unittest or unittest discover")
        options = arguments[2:]
        index = 0
        while index < len(options):
            option = options[index]
            if option in {"-v", "--verbose", "-q", "--quiet", "discover", "test_calculations", "test_calculations.py"}:
                index += 1
            elif option in {"-s", "--start-directory", "-p", "--pattern"} and index + 1 < len(options):
                accepted = {".", "/work"} if option in {"-s", "--start-directory"} else {"test*.py", "test_calculations.py"}
                if options[index + 1] not in accepted:
                    raise ValueError("unittest discovery scope is unverified")
                index += 2
            else:
                raise ValueError("unittest option or target is unverified")
        return
    if task_id == "B5.3":
        if len(segments) != 1 or len(first) != 2 or first[0] not in _SHELL or not _script_path(first[1], "build.sh"):
            raise ValueError("expected direct sh build.sh with separate stdout and stderr")
        return
    raise ValueError("unsupported B5 task")


def _check_observation(task_id: str, result: dict[str, Any]) -> None:
    expected_exit = 0 if task_id == "B5.1" else 1
    if type(result.get("exit_code")) is not int or result["exit_code"] != expected_exit:
        raise ValueError(f"expected observed exit code {expected_exit}")
    expected_status = "success" if expected_exit == 0 else "error"
    if result.get("status") != expected_status:
        raise ValueError("manager status does not match the command exit code")
    if expected_exit == 1 and result.get("error") != "command_error":
        raise ValueError("expected a command failure, not a sandbox or transport failure")
    if result.get("truncated") is not False:
        raise ValueError("complete, untruncated command output is required")
    stdout, stderr = result.get("stdout"), result.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError("manager stdout and stderr are required")
    if task_id == "B5.1":
        values = {}
        for line in stdout.strip().splitlines():
            match = re.fullmatch(r"(count|mean|median):\s*([-+0-9.eE]+)", line.strip())
            if not match or match[1] in values:
                raise ValueError("statistics output differs from the actual fixture tool")
            try:
                values[match[1]] = Decimal(match[2])
            except InvalidOperation as error:
                raise ValueError("statistics output contains an invalid number") from error
        if values != {"count": Decimal(8), "mean": Decimal("20.75"), "median": Decimal("9.5")} or stderr.strip():
            raise ValueError("the tool did not report the fixture's count, mean and median")
    elif task_id == "B5.2":
        combined = stdout + "\n" + stderr
        if not re.search(r"^Ran 20 tests in [0-9.]+s$", combined, re.MULTILINE) or not re.search(r"^FAILED \(failures=3\)$", combined, re.MULTILINE):
            raise ValueError("unittest must report 20 tests and exactly 3 failures")
        failures = re.findall(r"^FAIL: (\w+) \(([\w.]+)\)$", combined, re.MULTILINE)
        identifiers = {identifier for method, identifier in failures if identifier.rsplit(".", 1)[-1] == method}
        if len(failures) != 3 or identifiers != _FAILURES:
            raise ValueError("observed failing test identifiers do not match the fixture")
    elif stdout.strip() != "OK" or stderr.strip() != "ERROR: missing generated schema.json":
        raise ValueError("build output must include OK and its actual stderr failure reason")


def check_tool_evidence(task_id: str, trace: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    """Require a recognized command and its linked manager observation for B5.

    Returns an explicit unverified reason for unsupported equivalent commands.
    Artifact checkers must also pass. This never treats model prose, a WriteFile,
    or a tool result without the manager's matching operation identity as proof.
    """
    if not task_id.startswith("B5."):
        return True, "tool-execution evidence is not required for this task"
    if task_id not in {"B5.1", "B5.2", "B5.3"}:
        return False, "unsupported B5 task; tool execution is unverified"
    calls: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    reasons = []
    for event in trace:
        if not isinstance(event, dict):
            continue
        operation = event.get("operation_id")
        if not isinstance(operation, str) or not operation:
            continue
        if event.get("type") == "tool_call" and event.get("tool") == "RunCommand":
            previous = calls.get(operation)
            if previous is not None and previous != event:
                conflicts.add(operation)
            calls[operation] = event
            continue
        if event.get("type") != "tool_result" or operation not in calls or operation in conflicts:
            continue
        call = calls[operation]
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        if any(item.get("version") != "0" for item in (call, event, result)) or result.get("operation_id") != operation:
            reasons.append("result lacks the matching versioned manager operation identity")
            continue
        if not isinstance(call.get("trace_id"), str) or not call["trace_id"] or call["trace_id"] != event.get("trace_id"):
            reasons.append("call and result belong to different or missing traces")
            continue
        if type(call.get("seq")) is not int or type(event.get("seq")) is not int or event["seq"] <= call["seq"]:
            reasons.append("result does not follow its command in the server event sequence")
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict) or set(arguments) != {"command"} or not isinstance(arguments["command"], str):
            continue
        try:
            _command_kind(task_id, arguments["command"])
            _check_observation(task_id, result)
        except ValueError as error:
            reasons.append(str(error))
        else:
            return True, f"verified {task_id} RunCommand and matching manager result"
    reason = reasons[-1] if reasons else "no linked RunCommand and manager result"
    return False, "tool execution is unverified: " + reason
