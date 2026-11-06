"""Reject accidental provider calls before accepting a task or using model output."""
import importlib
import uuid

import httpx
import pytest
from fastapi import HTTPException

from client.cli import Client
from runtime.common import Journal, Run


@pytest.mark.parametrize('health,mode,model', [
    ({'model_mode': 'real', 'model': 'paid'}, 'mock', None),
    ({'model_mode': 'mock', 'model': None}, 'real', 'approved'),
    ({'model_mode': 'real', 'model': 'different'}, 'real', 'approved'),
    ({'status': 'ready', 'version': '0'}, 'mock', None),
    ({'model_mode': 'real', 'model': 'paid'}, 'real', None),
])
def test_client_rejects_mismatch_before_provisioning(tmp_path, health, mode, model):
    requests = []
    def handler(request):
        requests.append((request.method, request.url.path))
        assert request.method == 'GET' and request.url.path == '/health'
        return httpx.Response(200, json=health)
    client = Client()
    client.http.close()
    client.http = httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            client.execute(tmp_path, 'do work', tmp_path/'out', expected_mode=mode, expected_model=model)
        assert all(method == 'GET' for method, _ in requests)
        assert not (tmp_path/'out').exists()
    finally:
        client.http.close()


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    module = importlib.import_module('runtime.harness')
    journal = Journal('expectation-test')
    monkeypatch.setattr(module, 'journal', journal)
    monkeypatch.setattr(module, 'busy', {})
    monkeypatch.setattr(module, 'cancelled', set())
    yield module
    journal.db.close()


@pytest.mark.parametrize('actual,mode,model', [
    ({'mode':'real','model':'paid'}, 'mock', None),
    ({'mode':'mock','model':None}, 'real', 'approved'),
    ({'mode':'real','model':'other'}, 'real', 'approved'),
    ({'mode':'real','model':'approved'}, 'real', None),
])
def test_server_rejects_mismatch_before_run_is_accepted(harness, monkeypatch, actual, mode, model):
    calls = []
    def call(base, path, **kwargs):
        calls.append(path)
        assert path == '/health'
        return actual
    monkeypatch.setattr(harness, 'call', call)
    request = Run(request_id=uuid.uuid4().hex, session_id='s', instruction='do work',
                  expected_mode=mode, expected_model=model)
    with pytest.raises(HTTPException) as error:
        harness.run(request)
    assert error.value.status_code == 409
    assert calls == ['/health']
    with pytest.raises(HTTPException) as missing:
        harness.journal.get(request.request_id)
    assert missing.value.status_code == 404


def test_incomplete_model_output_is_recorded_but_never_executed(harness, monkeypatch):
    request = Run(request_id=uuid.uuid4().hex, session_id='s', instruction='do work',
                  expected_mode='real', expected_model='approved')
    harness.journal.reserve(request.request_id, request.model_dump(), {
        'version':'0','request_id':request.request_id,'session_id':'s','status':'running'})
    calls = []
    def call(base, path, body):
        calls.append(path)
        assert path == '/completion'
        assert body['expected_mode'] == 'real' and body['expected_model'] == 'approved'
        return {'mode':'real','model':'approved','usage':{'prompt_tokens':23,'completion_tokens':7},
                'error':'incomplete_response','message':{'role':'assistant','content':None,
                'tool_calls':[{'id':'t','function':{'name':'RunCommand','arguments':'{"command":"false"}'}}]}}
    monkeypatch.setattr(harness, 'call', call)
    harness.agent_loop(request)
    state = harness.journal.get(request.request_id)
    assert state['status'] == 'error' and state['error'] == 'model_error'
    events = harness.journal.events(request.request_id)
    assert [event['type'] for event in events] == ['model','result']
    assert events[0]['usage'] == {'prompt_tokens':23,'completion_tokens':7}
    assert calls == ['/completion']
