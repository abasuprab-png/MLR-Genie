exec(open('/tmp/mlr-independent/probe.py').read().split('from evals.audit_adversarial')[0])
from fastapi.testclient import TestClient
import backend.main as main
main.JOBS_DB.save_job(job('victim-private'))
client=TestClient(main.app)
for headers in ({'X-Actor-Id':'attacker'},{'Authorization':'nonsense'}):
 resp=client.get('/jobs/victim-private',headers=headers); print('API_CROSS_TENANT_WITHOUT_CREDENTIAL',headers,resp.status_code,resp.json().get('job',{}).get('tenant_id'))
from evals.acceptance_suite import run_all_12_acceptance_cases
from evals.pharma_comms_scenario_battery import run_pharma_comms_scenario_battery
for name,fn in [('acceptance',run_all_12_acceptance_cases),('scenario',run_pharma_comms_scenario_battery)]:
 res=fn(); print(name,len(res),sum(v.get('passed') is True for v in res.values()))
