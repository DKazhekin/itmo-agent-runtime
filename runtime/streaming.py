import json
import time


def stream_events(fetch, after=0, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in fetch(after):
            after = event['seq']
            yield f"id: {after}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            if event['type'] == 'result':
                return
        yield ': heartbeat\n\n'
        time.sleep(.2)
