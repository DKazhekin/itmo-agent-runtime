"""Check final workspace state independently of the model response."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_support import exact_text, guarded_file, path_set, read_text, run, unchanged
TASK = Path(__file__).resolve().parent
import json
from jsonschema import Draft202012Validator

def check(work: Path) -> None:
    unchanged(TASK, work)
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(read_text(work,"config.json"), object_pairs_hook=unique_keys)
    Draft202012Validator(json.loads((TASK/"schema.json").read_text())).validate(value)

if __name__ == "__main__":
    run(check)
