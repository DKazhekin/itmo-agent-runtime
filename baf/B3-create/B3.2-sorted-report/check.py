"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent
import csv
import io

def check(work: Path) -> None:
    unchanged(TASK, work)
    actual = list(csv.reader(io.StringIO(read_text(work,"report.csv"))))
    if actual != [['id', 'date', 'description', 'amount'], ['R01', '2025-12-31', 'Inventory', '300.00'], ['R02', '2026-02-03', 'Delivery', '75.00'], ['R03', '2026-03-17', 'Return "priority"', '-15.00'], ['R04', '2026-04-09', 'Packaging, reusable', '120.50'], ['R05', '2026-04-09', 'Storage', '44.25']]:
        raise ValueError("CSV content or row order differs from the reference")

if __name__ == "__main__":
    run(check)
