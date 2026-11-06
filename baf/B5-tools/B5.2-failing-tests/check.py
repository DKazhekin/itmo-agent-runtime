"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent

def check(work: Path) -> None:
    unchanged(TASK, work)
    path_set(work,"failed.txt",{'test_calculations.CalculationTests.test_empty_average', 'test_calculations.CalculationTests.test_negative_discount', 'test_calculations.CalculationTests.test_cent_rounding'})

if __name__ == "__main__":
    run(check)
