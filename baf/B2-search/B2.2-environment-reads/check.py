"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent

def check(work: Path) -> None:
    unchanged(TASK, work)
    path_set(work,"answer.txt",{'modules/component_017.py', 'modules/component_176.py', 'modules/component_193.py', 'modules/component_046.py', 'modules/component_079.py', 'modules/component_028.py', 'modules/component_137.py', 'modules/component_061.py', 'modules/component_094.py', 'modules/component_112.py', 'modules/component_003.py', 'modules/component_158.py'})

if __name__ == "__main__":
    run(check)
