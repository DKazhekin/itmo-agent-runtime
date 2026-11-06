"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent

def check(work: Path) -> None:
    unchanged(TASK, work, {"prices.csv"})
    expected = (TASK/"fixture/prices.csv").read_bytes().replace(b",1290\r\n",b",990\r\n")
    if guarded_file(work,"prices.csv").read_bytes() != expected:
        raise ValueError("Wrong price or changed cp1251/CRLF/other bytes")

if __name__ == "__main__":
    run(check)
