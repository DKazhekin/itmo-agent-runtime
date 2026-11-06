"""Remove only the isolated Linux verification project and check all owned resources."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid

import httpx


parser = argparse.ArgumentParser()
parser.add_argument('--source-directory', required=True)
args = parser.parse_args()
source = args.source_directory
if not source.startswith('/tmp/itmo-') or '..' in Path(source).parts:
    parser.error('source directory must be an isolated /tmp/itmo-* checkout')
root = Path(__file__).resolve().parents[1]
output = root / 'results/verification'
output.mkdir(parents=True, exist_ok=True)
session = 'cleanup-' + uuid.uuid4().hex[:16]
with httpx.Client(base_url='http://127.0.0.1:8081',
                  headers={'Authorization': 'Bearer local-checkpoint-token'},
                  timeout=30, trust_env=False) as client:
    response = client.post('/sessions', json={'version': '0', 'session_id': session, 'files': {}})
    response.raise_for_status()
    assert response.json()['status'] == 'ready'
name = 'itmo-clean-sb-' + session
assert subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], text=True).strip()
command = 'cd ' + shlex.quote(source) + ' && docker compose -p itmo-clean down -v'
process = subprocess.run(['colima', 'ssh', '--', 'sh', '-c', command], capture_output=True, text=True)
(output / 'checkpoint-final-down.txt').write_text(process.stdout + process.stderr)
assert process.returncode == 0, 'isolated project shutdown failed'
observations = []
deadline = time.monotonic() + 30
while True:
    checks = {}
    for resource in ('container', 'volume', 'network'):
        flags = ['-a', '-q'] if resource == 'container' else ['-q']
        remaining = subprocess.check_output(['docker', resource, 'ls', *flags, '--filter',
            'label=com.docker.compose.project=itmo-clean'], text=True).split()
        checks[resource + '_remaining'] = len(remaining)
    checks['sandbox_remaining'] = len(subprocess.check_output(['docker', 'ps', '-aq', '--filter',
        'label=itmo.checkpoint.sandbox=itmo-clean'], text=True).split())
    observations.append(checks)
    if not any(checks.values()) or time.monotonic() >= deadline:
        break
    time.sleep(0.25)
result = {'pass': not any(checks.values()), 'session': session,
          'checks': checks, 'observations': observations,
          'includes_stopped_containers': True}
(output / 'checkpoint-final-cleanup.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
assert result['pass'], 'isolated project resources remain after shutdown'
