"""Opt-in real HTTP + compiled TypeScript client + unmocked Gemini smoke test."""
from pathlib import Path
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time


def main():
    repo = Path(__file__).resolve().parents[1]
    run = Path(tempfile.mkdtemp(prefix="mlr-live-integrations-"))
    os.environ["DATABASE_URL"] = "sqlite:///" + str(run / "jobs.sqlite3")
    os.environ["MLR_REQUIRE_SIGNED_BEARER_TOKEN"] = "true"
    os.environ["MLR_REQUIRE_LIVE_GEMINI"] = "true"
    if "--hosted" in sys.argv:
        os.environ["MLR_GENIE_ENV"] = "hosted"
        # Isolated smoke-test credentials; never written to a report or shared configuration.
        os.environ.setdefault("MLR_GENIE_SESSION_SECRET", secrets.token_urlsafe(48))
        os.environ.setdefault("MLR_GENIE_BOOTSTRAP_SECRET", secrets.token_urlsafe(48))
        os.environ["MLR_GENIE_ALLOWED_ORIGINS"] = ""
    sys.path.insert(0, str(repo))
    import backend.main as api
    from backend.document_engine import create_born_digital_reference_pdf
    import uvicorn

    claim = "In adults with EGFR-mutated NSCLC, Aurenix reduced the risk of disease progression or death by 38% compared with placebo."
    pdf = run / "source.pdf"
    create_born_digital_reference_pdf(pdf, pages_spec=[
        {"left_column": ["This synthetic randomized trial enrolled adults with EGFR-mutated NSCLC."],
         "right_column": ["All study data in this document are fictional software verification inputs."]},
        {"left_column": [claim], "right_column": ["The trial evaluated the prespecified primary endpoint."]},
    ])
    subprocess.run([str(repo / "node_modules/.bin/tsc"), str(repo / "integrations/camsCommsRxClient.ts"),
                    "--target", "ES2022", "--module", "ESNext", "--lib", "ES2022,DOM", "--strict",
                    "--skipLibCheck", "--outDir", str(run)], check=True, cwd=repo)
    (run / "package.json").write_text('{"type":"module"}')
    (run / "client.mjs").write_text('''
import { readFile, writeFile } from 'node:fs/promises';
import { verifyCamsSource, runCommsRxRoundtrip } from './camsCommsRxClient.js';
const baseUrl = process.env.MLR_INTEGRATION_BASE_URL;
const authHeaders = { 'Content-Type': 'application/json' };
if (process.env.MLR_GENIE_BOOTSTRAP_SECRET) authHeaders['X-MLR-Bootstrap-Secret'] = process.env.MLR_GENIE_BOOTSTRAP_SECRET;
async function issue(actor_id,role) {
  const auth = await fetch(baseUrl + '/auth/token', {method:'POST', headers:authHeaders,
    body:JSON.stringify({tenant_id:'synthetic-integrations',actor_id,role})});
  if (!auth.ok) throw new Error('Token endpoint failed: ' + auth.status);
  return (await auth.json()).access_token;
}
const serviceToken = await issue('synthetic-service','service');
const reviewerToken = await issue('synthetic-reviewer','reviewer');
const options = {baseUrl, token:reviewerToken, signal:AbortSignal.timeout(300000)};
const referencePdf = new Uint8Array(await readFile(new URL('./source.pdf', import.meta.url)));
const claim = JSON.parse(await readFile(new URL('./input.json', import.meta.url), 'utf8')).claim;
const cams = await verifyCamsSource({...options,token:serviceToken}, {claimText:claim, referencePdf, requireLive:true});
if (cams.anchor.verdict !== 'supported' || cams.anchor.page !== 2) throw new Error('CAMS anchor did not verify');
console.log('Live CAMS source verified through TypeScript client.');
const roundtrip = await runCommsRxRoundtrip(options, {bodyText:claim, claims:[{id:'claim-external',text:claim}],
  referencePdf, requireLive:true, comments:[{commentId:'medical',claimId:'claim-external',reviewerName:'Synthetic Medical',
    reviewerRole:'Medical',timestamp:'2026-10-30T12:00:00Z',anchoredPassage:claim,commentText:'Use lowered the risk.'}],
  acceptedEdits:[{commentId:'medical',edits:[{targetText:'reduced the risk',replacementText:'lowered the risk'}]}]});
const {files,...result} = roundtrip;
for (const [name,blob] of Object.entries(files)) await writeFile(new URL('./'+name,import.meta.url), new Uint8Array(await blob.arrayBuffer()));
await writeFile(new URL('./result.json',import.meta.url), JSON.stringify({cams,roundtrip:result,
  authentication:{mode:process.env.MLR_GENIE_ENV || 'local',bootstrap_issued:Boolean(process.env.MLR_GENIE_BOOTSTRAP_SECRET),
    evidence_role:'service',acceptance_role:'reviewer'}},null,2));
console.log('All four CommsRx artifacts downloaded through TypeScript client.');
''')
    (run / "input.json").write_text(json.dumps({"claim": claim}))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(api.app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Local server did not start")
            time.sleep(0.05)
        env = {**os.environ, "MLR_INTEGRATION_BASE_URL": f"http://127.0.0.1:{sock.getsockname()[1]}"}
        subprocess.run(["node", str(run / "client.mjs")], env=env, check=True, cwd=run)
        import docx
        import zipfile
        import pymupdf
        assert "lowered the risk" in "\n".join(p.text for p in docx.Document(run / "clean_revised.docx").paragraphs)
        with zipfile.ZipFile(run / "redline.docx") as redline:
            xml = redline.read("word/document.xml").decode()
            assert "<w:del " in xml and "<w:ins " in xml
            assert 'w:author="synthetic-reviewer"' in xml and 'w:author="synthetic-service"' not in xml
        with pymupdf.open(run / "audit_summary.pdf") as audit:
            audit_text = "".join(page.get_text() for page in audit)
            assert "accepted" in audit_text and "synthetic-reviewer" in audit_text and "synthetic-service" not in audit_text
        with pymupdf.open(run / "annotated_source.pdf") as annotated:
            assert len(annotated) == 2 and any(list(p.annots() or []) for p in annotated)
        telemetry = [t.model_dump() for t in api.router.telemetry_log]
        assert telemetry and all(t["used_live_sdk"] and t["transport_mode"] == "generate_content" for t in telemetry)
        result = json.loads((run / "result.json").read_text())
        reviewed_job = api.JOBS_DB[result["roundtrip"]["job_id"]]
        assert reviewed_job.comments[0].human_actor == "synthetic-reviewer"
        result["acceptance_provenance"] = reviewed_job.metadata["accepted_comment_authorizations"]
        for name, artifact in result["roundtrip"]["artifacts"].items():
            assert hashlib.sha256((run / name).read_bytes()).hexdigest() == artifact["sha256"]
        result["router_telemetry_log"] = telemetry
        result["source_sha256"] = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in [*sorted((repo / "backend").rglob("*.py")), repo / "integrations/camsCommsRxClient.ts"]}
        (run / "result.json").write_text(json.dumps(result, indent=2))
        print(f"Passed: {len(telemetry)} live calls; report: {run / 'result.json'}")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
