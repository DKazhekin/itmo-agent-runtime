"""Shared independent checks for final workspace artifacts."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from typing import Callable

MAX_OUTPUT_BYTES = 1_048_576

def guarded_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Workspace must be a real directory")
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"Symlinks are not accepted: {relative}")
    if not candidate.is_file():
        raise ValueError(f"Missing regular file: {relative}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact escapes workspace")
    if candidate.stat().st_size > MAX_OUTPUT_BYTES:
        raise ValueError(f"Artifact too large: {relative}")
    return candidate

def read_text(root: Path, relative: str) -> str:
    return guarded_file(root, relative).read_text(encoding="utf-8")

def unchanged(task: Path, work: Path, except_paths: set[str] | None = None) -> None:
    exceptions = except_paths or set()
    for original in (task / "fixture").rglob("*"):
        if original.is_file():
            relative = original.relative_to(task / "fixture").as_posix()
            if relative not in exceptions:
                actual = guarded_file(work, relative).read_bytes()
                if actual != original.read_bytes():
                    raise ValueError(f"Unexpected source modification: {relative}")

def exact_text(work: Path, name: str, expected: str) -> None:
    actual = read_text(work, name).strip()
    if actual != expected:
        raise ValueError(f"Expected {expected!r}, received {actual!r}")

def path_set(work: Path, name: str, expected: set[str]) -> None:
    lines = read_text(work, name).strip().splitlines()
    actual = {line.strip().removeprefix("./") for line in lines}
    if actual != expected:
        raise ValueError(f"Incorrect set: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")

def run(check: Callable[[Path], None]) -> None:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: python check.py WORKDIR")
        check(Path(sys.argv[1]))
    except Exception as error:
        print(json.dumps({"pass": False, "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({"pass": True, "reason": "ok"}))
