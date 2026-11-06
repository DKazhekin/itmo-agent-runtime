"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent

def check(work: Path) -> None:
    unchanged(TASK, work)
    answer = read_text(work,"answer.txt").strip()
    lines = answer.splitlines()
    if not lines or lines[0].strip().split(maxsplit=1)[0].lower().strip(":,.;") != "no":
        raise ValueError("Expected no despite the OK message on stdout")
    if "missing generated schema.json" not in answer.lower():
        raise ValueError("The stderr failure reason is missing")

if __name__ == "__main__":
    run(check)
