"""Real local HTTP requests and unmocked Gemini SDK calls; synthetic inputs only."""
from pathlib import Path
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import tempfile
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
run = Path(os.getenv('MLR_LIVE_REPORT_DIR', tempfile.gettempdir())) / ('run-' + uuid.uuid4().hex[:10])
run.mkdir(parents=True)
os.environ['MLR_REQUIRE_LIVE_GEMINI'] = 'true'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(run / 'live.sqlite3')
# This isolated evaluator binds its own loopback socket and uses synthetic data.
os.environ.setdefault('MLR_GENIE_ENV', 'local')
if '--bootstrap-probe' in sys.argv:
    os.environ['MLR_GENIE_BOOTSTRAP_SECRET'] = secrets.token_urlsafe(32)
sys.path.insert(0, str(REPO))

import docx
import httpx
import pymupdf
import uvicorn
import backend.main as main
from backend.document_engine import create_born_digital_reference_pdf, inspect_pdf_document_and_extract_source_pack
from backend.domain.schemas import JobRecord, SectorType

redactions = [os.environ[n] for n in ('GEMINI_API_KEY', 'MLR_GENIE_BOOTSTRAP_SECRET', 'MLR_GENIE_SESSION_SECRET') if os.environ.get(n)]

def sanitize(value):
    text = json.dumps(value, default=str)
    for secret in redactions:
        text = text.replace(secret, '[REDACTED]')
    return json.loads(text)

report = {
    'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
    'working_tree': subprocess.check_output(['git', 'status', '--short'], cwd=REPO, text=True).strip(),
    'client_type': type(main.router._client).__module__ + '.' + type(main.router._client).__name__,
    'steps': [], 'provider_errors': [], 'model_returns': [], 'red_team': {},
    'source_sha256': {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted((REPO / 'backend').rglob('*.py'))},
}

def save():
    report['router_telemetry_log'] = [item.model_dump() for item in main.router.telemetry_log]
    (run / 'report.json').write_text(json.dumps(sanitize(report), indent=2))

def observe(frame, event, value):
    # Observe returned provider errors only; never substitute calls, inputs, or outputs.
    if event == 'return' and frame.f_code.co_name == 'create_interaction' and frame.f_code.co_filename == str(REPO / 'backend/agent/models_router.py'):
        if isinstance(value, tuple):
            report['model_returns'].append(sanitize({'task': frame.f_locals.get('task_type'), 'payload': value[0], 'telemetry': value[1].model_dump()}))
        if isinstance(value, tuple) and isinstance(value[0], dict) and value[0].get('status') == 'provider_error':
            report['provider_errors'].append(sanitize({'task': frame.f_locals.get('task_type'), 'error': value[0].get('error')}))

threading.setprofile(observe)
sys.setprofile(observe)
claim = 'In adults with EGFR-mutated NSCLC, Aurenix reduced the risk of disease progression or death by 38% compared with placebo.'
isi = 'Important Safety Information: Aurenix is contraindicated in patients with severe hepatic impairment.'
body = claim + '\n' + isi
pdf = run / 'two-column-reference.pdf'
create_born_digital_reference_pdf(pdf, pages_spec=[
    {'header': 'Fictional AURORA clinical study: design', 'left_column': [
        'This synthetic randomized study enrolled adults with EGFR-mutated NSCLC. The comparison treatment was placebo. All figures in this document are fictional and are used for software verification.',
        'The primary endpoint was disease progression or death. Assessors evaluated the endpoint using the same schedule in both treatment groups. The protocol specified an adult study population.',
    ], 'right_column': [
        'Randomization allocated participants to Aurenix or placebo. The study did not enroll children. Treatment continued according to the study protocol until the prespecified stopping criteria were met.',
        'The study team collected efficacy and safety information separately. The synthetic reference document does not establish findings beyond the reported population and endpoint.',
    ]},
    {'header': 'Fictional AURORA clinical study: findings', 'left_column': [claim], 'right_column': [isi]},
])
source_pack = inspect_pdf_document_and_extract_source_pack(pdf, source_id='LIVE-SYNTHETIC-PDF', version='1')['source_pack']
document = docx.Document()
document.add_paragraph(claim)
document.add_paragraph(isi)
document.save(run / 'draft.docx')

sock = socket.socket()
sock.bind(('127.0.0.1', 0))
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(main.app, log_level='error', access_log=False))
worker = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
worker.start()
deadline = time.monotonic() + 15
while not server.started:
    if time.monotonic() > deadline:
        raise RuntimeError('Local API server did not start')
    time.sleep(0.05)

client = httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=180)
headers = {}
job_id = 'job_live_' + run.name[4:]

def request(name, path, **kwargs):
    start = len(main.router.telemetry_log)
    response = client.post(path, headers=headers, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {'error': response.text[:1000]}
    entry = {'step': name, 'status_code': response.status_code, 'telemetry': [t.model_dump() for t in main.router.telemetry_log[start:]]}
    if path != '/auth/token':
        entry['response'] = sanitize(payload)
    report['steps'].append(entry)
    save()
    print(json.dumps({'step': name, 'status': response.status_code, 'calls': [(t['task_type'], t['used_live_sdk'], t['transport_mode']) for t in entry['telemetry']]}), flush=True)
    response.raise_for_status()
    return payload

def intake(which_id, text, sources):
    job = JobRecord(job_id=which_id, tenant_id='synthetic_live_tenant', title='Synthetic live verification', sector=SectorType.PHARMA_APPROVED, document_type='Brief', destination_platform='package_mode', deadline_iso='2026-12-01T00:00:00Z')
    return request('intake:' + which_id, '/jobs/intake', json={'job': job.model_dump(), 'initial_body': text, 'actor': 'synthetic_reviewer', 'source_pack': sources})

try:
    if os.environ.get('MLR_GENIE_BOOTSTRAP_SECRET'):
        token_body = {'tenant_id': 'synthetic_live_tenant', 'actor_id': 'synthetic_reviewer'}
        missing = client.post('/auth/token', json=token_body)
        wrong = client.post('/auth/token', json=token_body, headers={'X-MLR-Bootstrap-Secret': 'incorrect-bootstrap-probe'})
        report['bootstrap_gate'] = {'missing_header': missing.status_code, 'wrong_header': wrong.status_code}
        assert missing.status_code == 401 and wrong.status_code == 403
        headers['X-MLR-Bootstrap-Secret'] = os.environ['MLR_GENIE_BOOTSTRAP_SECRET']
    auth = request('token', '/auth/token', json={'tenant_id': 'synthetic_live_tenant', 'actor_id': 'synthetic_reviewer', 'role': 'reviewer', 'ttl_seconds': 3600})
    redactions.append(auth['access_token'])
    headers = {'Authorization': 'Bearer ' + auth['access_token']}
    initial = intake(job_id, body, source_pack)
    assert initial['job']['state'] == 'in_review'
    assert initial['job']['safety_findings'] and initial['job']['claims']
    report['initial_review_state'] = initial['job']['state']
    request('upload-docx', f'/jobs/{job_id}/upload-working-docx', files={'file': ('working.docx', (run / 'draft.docx').read_bytes(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
    uploaded = request('upload-pdf', f'/jobs/{job_id}/upload-reference-pdf', files={'file': ('reference.pdf', pdf.read_bytes(), 'application/pdf')}, data={'source_id': 'LIVE-SYNTHETIC-PDF', 'version': '1'})
    claims = uploaded.get('job', {}).get('claims', [])
    report['red_team']['supported_claim'] = {'claims': claims, 'telemetry_receipts': uploaded.get('job', {}).get('metadata', {}).get('telemetry_receipts', []), 'pdf_pages': uploaded.get('reference_pdf', {}).get('page_count')}
    comments = [
        {'comment_id': 'medical', 'reviewer_name': 'Synthetic Medical Reviewer', 'reviewer_role': 'Medical', 'timestamp': '2026-10-30T12:00:00Z', 'anchored_passage': claim, 'comment_text': 'Use lowered rather than reduced while preserving the endpoint and estimate.', 'category': 'editorial_request', 'proposed_spans': [{'target_text': 'reduced the risk', 'replacement_text': 'lowered the risk'}]},
        {'comment_id': 'regulatory', 'reviewer_name': 'Synthetic Regulatory Reviewer', 'reviewer_role': 'Regulatory', 'timestamp': '2026-10-30T12:01:00Z', 'anchored_passage': claim, 'comment_text': 'Delete the benefit statement pending the updated approved label.', 'category': 'editorial_request', 'proposed_spans': [{'target_text': claim, 'replacement_text': 'The benefit statement is pending label review.'}]},
    ]
    conflict = request('conflicting-comments', f'/jobs/{job_id}/comments', json=comments)
    report['red_team']['comments'] = {'initial_reconciliation': conflict.get('reconciliation')}
    label = request('diff-label', f'/jobs/{job_id}/diff-label', json={
        'prepared_scenarios': [{'scenario_id': 'direct_mechanism', 'section_12_1_moa': 'Aurenix directly blocks receptor signaling.'}],
        'final_approved_label': {'section_12_1_moa': 'Aurenix is thought to reduce receptor signaling; the mechanism in humans has not been fully established.', 'section_14_clinical_figures': [{'endpoint': 'disease progression or death', 'old_value': '38%', 'new_value': '35%'}]},
        'launch_kit_assets': [{'asset_id': 'launch_brief', 'body': body + '\nAurenix directly blocks receptor signaling.'}],
    })
    report['red_team']['label_diff'] = label.get('label_delta')
    comments[0].update(human_decision='accepted', human_actor='synthetic_reviewer')
    comments[1].update(human_decision='rejected', human_actor='synthetic_reviewer')
    accepted = request('accept-one-edit', f'/jobs/{job_id}/comments', json=comments)
    report['red_team']['comments']['after_acceptance'] = accepted.get('reconciliation')
    report['red_team']['comments']['decisions_after_acceptance'] = [{k: c.get(k) for k in ('comment_id', 'human_decision', 'placement_status')} for c in accepted.get('job', {}).get('comments', [])]
    roundtrip = request('roundtrip', f'/jobs/{job_id}/execute-file-roundtrip')
    report['red_team']['comments']['artifacts'] = roundtrip.get('artifacts')
    artifact_dir = REPO / 'artifacts/file_roundtrip' / job_id
    redline = artifact_dir / '05_tracked_changes_redline_v1_to_v2.docx'
    if redline.exists():
        with zipfile.ZipFile(redline) as z:
            xml = z.read('word/document.xml').decode()
            report['red_team']['comments']['redline_xml'] = {'has_w_del': '<w:del ' in xml, 'has_w_ins': '<w:ins ' in xml}
    report['last_telemetry_receipts'] = main.JOBS_DB[job_id].metadata.get('telemetry_receipts', [])
    if any(t.used_live_sdk for t in main.router.telemetry_log):
        shifted = intake(job_id + '_shift', body.replace('In adults', 'In children'), source_pack)
        shifted = request('shift-upload-pdf', f'/jobs/{job_id}_shift/upload-reference-pdf', files={'file': ('reference.pdf', pdf.read_bytes(), 'application/pdf')}, data={'source_id': 'LIVE-SYNTHETIC-PDF', 'version': '1'})
        report['red_team']['population_shift'] = {'claims': shifted.get('job', {}).get('claims'), 'telemetry_receipts': shifted.get('job', {}).get('metadata', {}).get('telemetry_receipts', [])}
        pv = intake(job_id + '_pv', 'My aunt became so light-headed that she crumpled to the kitchen floor after swallowing her first Aurenix tablet; paramedics took her straight to hospital.', [])
        report['red_team']['paraphrased_adverse_event'] = {'screening': pv.get('pv_screening'), 'state': pv.get('job', {}).get('state'), 'safety_findings': pv.get('job', {}).get('safety_findings')}
    else:
        report['red_team']['population_shift'] = {'status': 'blocked: no successful live SDK calls'}
        report['red_team']['paraphrased_adverse_event'] = {'status': 'blocked: no successful live SDK calls'}
    from backend.domain.schemas import ClaimVerificationVerdict
    verdicts = [m for m in report['model_returns'] if m['task'] == 'claim_verification']
    for result in verdicts:
        ClaimVerificationVerdict.model_validate(result['payload'])
    report['valid_claim_verdicts'] = len(verdicts)
    supported = next(c for c in claims if c['sentence_text'] == claim)
    anchor = supported['anchors'][0]
    box = [anchor[k] for k in ('page_number', 'x0', 'y0', 'x1', 'y1')]
    with pymupdf.open(pdf) as reference:
        page = reference[box[0]-1]
        rect = pymupdf.Rect(*box[1:])
        assert rect in page.rect
        clipped = page.get_text('text', clip=rect + (-1, -1, 1, 1))
        assert ' '.join(clipped.split()) == ' '.join(anchor['verbatim_quote'].split())
        assert len(reference) == 2
    report['verified_pdf_box'] = box
    revised = artifact_dir / '04_revised_clean_v2.docx'
    clean_text = '\n'.join(p.text for p in docx.Document(revised).paragraphs)
    assert 'lowered the risk' in clean_text and 'reduced the risk' not in clean_text
    assert clean_text == main.JOBS_DB[job_id].revisions[-1].body
    with zipfile.ZipFile(redline) as z:
        root = ET.fromstring(z.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        deleted = ''.join(root.find('.//w:del', ns).itertext())
        inserted = ''.join(root.find('.//w:ins', ns).itertext())
        assert deleted and inserted and deleted != inserted
    with zipfile.ZipFile(artifact_dir / '06_resubmission_package.zip') as z:
        assert z.testzip() is None
    with pymupdf.open(artifact_dir / '03_annotated_reference.pdf') as annotated:
        assert sum(len(list(page.annots() or [])) for page in annotated) > 0
    report['artifact_validation'] = {'clean_matches_job': True, 'deleted_text': deleted,
                                     'inserted_text': inserted, 'zip_valid': True, 'pdf_annotated': True}
    diff = report['red_team']['label_diff']
    impacted_sections = {i['section'] for i in diff['model_assessment']['impacts'] if i['impact'] != 'none'}
    assert impacted_sections == {'section_12_1_moa', 'section_14_clinical_figures'}
    findings = report['red_team']['paraphrased_adverse_event']
    outcomes = {
        'supported_pdf_claim': supported['support_status'] == 'supported' and anchor['anchor_valid'],
        'population_shift': any(c['support_status'] in ('contradicted', 'partially_supported') for c in shifted['job']['claims'] if 'children' in c['sentence_text']),
        'live_label_delta': diff['used_live_sdk'] and diff['label_quotes_verified'] and not diff['fits_prepared_scenario'] and any(a.get('model_impacts') for a in diff['affected_assets']),
        'human_resolution_and_redline': conflict['reconciliation']['requires_human_resolution'] and conflict['reconciliation']['used_live_sdk'] and not accepted['reconciliation']['requires_human_resolution'] and accepted['reconciliation']['used_live_sdk'] and report['red_team']['comments']['redline_xml']['has_w_del'] and report['red_team']['comments']['redline_xml']['has_w_ins'],
        'paraphrased_adverse_event': findings['screening']['cv08_flagged'] and findings['screening']['used_live_sdk'] and findings['state'] == 'hold' and bool(findings['safety_findings']),
    }
    report['outcomes'] = outcomes
    assert all(t.used_live_sdk and t.transport_mode == 'generate_content' for t in main.router.telemetry_log)
    assert all(outcomes.values()), outcomes
except Exception as exc:
    report['fatal_error'] = sanitize({'type': type(exc).__name__, 'message': str(exc)})
finally:
    save()
    client.close()
    server.should_exit = True
    worker.join(timeout=5)
    sys.setprofile(None)
    threading.setprofile(None)
    print('REPORT ' + str(run / 'report.json'), flush=True)
    print(json.dumps(sanitize({'provider_errors': report['provider_errors'], 'fatal_error': report.get('fatal_error')})), flush=True)

if report.get("fatal_error") or not all(report.get("outcomes", {}).values()) or not report.get("outcomes"):
    raise SystemExit(1)
