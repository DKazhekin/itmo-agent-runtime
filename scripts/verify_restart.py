import json,subprocess,uuid,time,os
from pathlib import Path
import httpx
project=Path(__file__).resolve().parents[1]
output = project / 'results/verification'
output.mkdir(parents=True, exist_ok=True)
client=httpx.Client(base_url='http://127.0.0.1:8080',headers={'Authorization':'Bearer local-checkpoint-token'},timeout=30)
session='restart-'+uuid.uuid4().hex[:16]
payload={'version':'0','session_id':session,'files':{}}
response=client.post('/sessions',json=payload);response.raise_for_status();assert response.json()['status']=='ready'
name='itmo-checkpoint-sb-'+session
def present():
 return subprocess.check_output(['docker','ps','-aq','--filter','name=^/'+name+'$'],text=True).strip()
assert present()
restart=subprocess.run(['sh',str(project/'scripts/compose.sh'),'restart','-t','5','sandbox-manager'],cwd=project,text=True,capture_output=True)
assert restart.returncode==0,restart.stderr
for _ in range(100):
 try:
  if client.get('/health').status_code==200: break
 except httpx.HTTPError: pass
 time.sleep(.2)
else: raise RuntimeError('restart failed readiness')
assert not present()
response=client.post('/sessions',json=payload);response.raise_for_status();assert response.json()['status']=='released',response.text
client.delete('/sessions/'+session).raise_for_status()
result={'kind':'host_readonly_container_inspection_and_targeted_service_restart','session':session,'container_before':True,'container_after':False,'repeated_provision_status':response.json(),'restart_exit':restart.returncode,'pass':True}
(output / 'restart.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
