"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent
import ast
from hidden_tests import verify

def check(work: Path) -> None:
    python_files = [source for source in (TASK/"fixture").rglob("*.py")]
    changed = {source.relative_to(TASK/"fixture").as_posix() for source in python_files}
    unchanged(TASK, work, changed)
    for source in python_files:
        relative = source.relative_to(TASK/"fixture").as_posix()
        actual = read_text(work, relative)
        expected = source.read_text().replace("calc_total", "compute_total")
        if "calc_total" in actual:
            raise ValueError(f"Old symbol remains in {relative}")
        if ast.dump(ast.parse(actual)) != ast.dump(ast.parse(expected)):
            raise ValueError(f"Unexpected semantic change in {relative}")
    for candidate in work.rglob("*.py"):
        if "calc_total" in read_text(work, candidate.relative_to(work).as_posix()):
            raise ValueError("Old name remains in project code")
    # The AST was compared to trusted source before execution. User-added code is never executed.
    namespace = {"__builtins__": {"sum": sum, "round": round}}
    exec(compile(read_text(work,"billing/totals.py"), "validated_totals.py", "exec"), namespace)
    verify(namespace["compute_total"])

if __name__ == "__main__":
    run(check)
