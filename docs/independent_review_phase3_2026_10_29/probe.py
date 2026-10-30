import os, sys, json
os.environ['GEMINI_API_KEY']=''; os.environ['GOOGLE_GENAI_LIVE_VERTEX']='false'; os.environ['DATABASE_URL']='sqlite:////tmp/mlr-independent/jobs.db'; sys.dont_write_bytecode=True
sys.path.insert(0,'/workspace/scratch/c0d7f6b3c9d9/mlr_handoff_2234/source')
from backend.domain.schemas import *
from backend.tools.document_tools import *
from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
r=GeminiModelRouter(GcpAgentConfig(use_vertex_ai=False,gemini_api_key=None))
def job(id): return JobRecord(job_id=id,tenant_id='victim',title='test',sector=SectorType.PHARMA_PREAPPROVAL,document_type='Press Release',destination_platform='package_mode',deadline_iso='2026-11-01T00:00:00Z')
def check(label,claim,source):
 j=job(label); ingest_working_document(job=j,body_text=claim,actor='test'); cs=locate_and_anchor_evidence(job=j,source_pack=[dict(source_id='src',version='1',verbatim_text=source,keywords=['aurenix'],bbox=dict(page=1,x0=0,y0=0,x1=100,y1=100))],router=r)
 print(label,[(c.support_status.value,c.plain_english_rationale,c.population,c.timeframe) for c in cs]); return j
check('population','Aurenix improved overall survival in children.','Aurenix improved overall survival in adults.')
check('direction','Aurenix decreased overall survival.','Aurenix increased overall survival.')
check('time_unit','Aurenix improved overall survival at 24 months.','Aurenix improved overall survival at 24 weeks.')
check('number_role','Aurenix showed response in 20% and toxicity in 60%.','Aurenix showed response in 60% and toxicity in 20%.')
j=check('stale','Aurenix improved overall survival.','Aurenix improved overall survival.')
c=ReviewComment(comment_id='c',reviewer_name='Medical',reviewer_role='Medical',timestamp='2026-10-29T00:00:00Z',anchored_passage=j.revisions[-1].body,comment_text='edit',category=CommentCategory.EDITORIAL_REQUEST,human_decision='accepted',proposed_spans=[WordingSpanEdit(target_text='improved',replacement_text='worsened')])
apply_accepted_changes(job=j,comments=[c],human_actor='test')
print('stale_after_apply',j.current_version,j.revisions[-1].body,[(x.sentence_text,x.support_status.value) for x in j.claims])
from backend.main import _require_authenticated_tenant
from starlette.requests import Request
for hs in [[(b'x-actor-id',b'impersonated-reviewer')],[(b'authorization',b'nonsense')],[(b'x-tenant-id',b'victim')]]:
 req=Request({'type':'http','headers':hs,'query_string':b''})
 print('auth',hs,_require_authenticated_tenant(req,expected_tenant_id='victim'))
from evals.audit_adversarial import run_all_27_adversarial_probes
res=run_all_27_adversarial_probes(); print('EXISTING_27',json.dumps(res,default=str))
