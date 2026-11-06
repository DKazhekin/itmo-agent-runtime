"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent
import yaml

def check(work: Path) -> None:
    unchanged(TASK, work)
    countries = yaml.safe_load(read_text(work,"countries.yaml"))
    postal = yaml.safe_load(read_text(work,"postal.yaml"))
    if countries != {"countries":["NO","ON","FR"]} or postal != {"postal_codes":{"central":"0123","west":"0042"}}:
        raise ValueError("YAML values or scalar types differ from the specification")
    if any(type(value) is not str for value in countries["countries"] + list(postal["postal_codes"].values())):
        raise ValueError("All codes must remain strings")

if __name__ == "__main__":
    run(check)
