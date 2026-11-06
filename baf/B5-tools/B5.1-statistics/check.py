"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent
from decimal import Decimal

def check(work: Path) -> None:
    unchanged(TASK, work)
    value = Decimal(read_text(work,"answer.txt").strip())
    if not value.is_finite() or value != Decimal("9.5"):
        raise ValueError("The median is incorrect")

if __name__ == "__main__":
    run(check)
