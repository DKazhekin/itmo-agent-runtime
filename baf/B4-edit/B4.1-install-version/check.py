"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent

def check(work: Path) -> None:
    unchanged(TASK, work, {"README.md"})
    original = (TASK/"fixture/README.md").read_bytes()
    expected = original.replace(b"pip install report-toolkit==1.2", b"pip install report-toolkit==1.3")
    if guarded_file(work,"README.md").read_bytes() != expected:
        raise ValueError("Only the Install command version may change")

if __name__ == "__main__":
    run(check)
