"""Check non-root budget persistence across a targeted proxy restart, without inference."""
import json
from pathlib import Path
import subprocess


root = Path(__file__).resolve().parents[1]
output = root / 'results/verification'
output.mkdir(parents=True, exist_ok=True)
compose = ["sh", str(root / "scripts/compose.sh")]


def inside(source):
    output = subprocess.check_output(compose + ["exec", "-T", "llm-proxy", "python", "-c", source],
                                     cwd=root, text=True)
    return json.loads(output)


before = inside('''import json, os
from pathlib import Path
from runtime.budget import BudgetLedger
assert os.getuid() == 1000
path = Path('/model-state/storage-verification.sqlite3')
assert not path.exists()
ledger = BudgetLedger(path, 'storage-verification', '1')
ledger.reserve(350000)
print(json.dumps(ledger.snapshot()))
''')
subprocess.run(compose + ["restart", "-t", "5", "llm-proxy"], cwd=root, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
after = inside('''import json, os
from pathlib import Path
from runtime.budget import BudgetLedger, BudgetError
assert os.getuid() == 1000
path = Path('/model-state/storage-verification.sqlite3')
ledger = BudgetLedger(path, 'storage-verification', '1', initialize=False)
state = ledger.snapshot()
assert state['reserved_micro'] == 350000
path.unlink()
try:
    BudgetLedger(path, 'storage-verification', '1', initialize=False)
except BudgetError:
    state['deleted_journal_rejected'] = True
else:
    raise AssertionError('deleted journal was recreated')
assert not path.exists()
print(json.dumps(state))
''')
assert before['reserved_micro'] == after['reserved_micro'] == 350000
result = {'pass': True, 'uid': 1000, 'reserved_before_micro': before['reserved_micro'],
          'reserved_after_restart_micro': after['reserved_micro'],
          'deleted_journal_rejected': after['deleted_journal_rejected'], 'model_requests': 0}
(output / 'budget-storage.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
