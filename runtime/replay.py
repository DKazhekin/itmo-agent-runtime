"""Explicit hand-authored infrastructure replay, never model-quality evidence."""
import json
import re


def response(messages):
    prompt = next(m['content'] for m in messages if m['role'] == 'user')
    marker = re.search(r'\[replay:(B[1-5]\.1)\]', prompt)
    if not marker or marker[1] == 'B1.1':
        return None
    case = marker[1]
    step = sum(m['role'] == 'tool' for m in messages)
    plans = {
        'B2.1': [('RunCommand', {'command': "grep -rl '^def parse_date' --include='*.py' . | sed 's|^./||' > answer.txt"})],
        'B3.1': [('ReadFile', {'path': 'spec.md'}), ('WriteFile', {'path': 'config.json', 'content': json.dumps({
            'service': 'inventory', 'environment': 'staging', 'listen': {'host': '127.0.0.1', 'port': 8137},
            'retries': 3, 'enabled': True, 'tags': ['internal', 'read-only']})})],
        'B4.1': [('RunCommand', {'command': "python -c \"from pathlib import Path; p=Path('README.md'); s=p.read_text(); before,section=s.split('## Install',1); section,after=section.split('## History',1); p.write_text(before+'## Install'+section.replace('==1.2','==1.3')+'## History'+after)\""})],
        'B5.1': [('RunCommand', {'command': "python stats.py data.csv > /tmp/stats-output; cat /tmp/stats-output; sed -n 's/^median: //p' /tmp/stats-output > answer.txt"})],
    }
    if step >= len(plans[case]):
        message = {'role': 'assistant', 'content': 'Replay finished'}
    else:
        name, arguments = plans[case][step]
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'replay-{step}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
    return {'message': message, 'mode': 'mock', 'usage': {}}
