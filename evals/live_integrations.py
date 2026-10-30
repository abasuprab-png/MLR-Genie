"""Opt-in real HTTP + compiled TypeScript client + unmocked Gemini smoke test."""
import argparse
from contextlib import contextmanager, nullcontext
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
from urllib.parse import urlsplit


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosted", action="store_true", help="Run the isolated local server in hosted mode.")
    parser.add_argument("--base-url", default=os.getenv("MLR_INTEGRATION_BASE_URL"),
                        help="Test an existing external HTTPS entry point instead of starting a local server.")
    args = parser.parse_args()
    if args.base_url:
        try:
            parsed = urlsplit(args.base_url)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            parser.error("--base-url must be an HTTPS URL without credentials, query, or fragment.")
        if not os.getenv("MLR_GENIE_BOOTSTRAP_SECRET", "").strip():
            parser.error("External evaluation requires the deployed MLR_GENIE_BOOTSTRAP_SECRET; no credentials are generated.")
        args.base_url = args.base_url.rstrip("/")
    return args


@contextmanager
def local_server(run, hosted):
    # These settings belong only to this isolated process, never a remote service.
    os.environ["DATABASE_URL"] = "sqlite:///" + str(run / "jobs.sqlite3")
    os.environ["MLR_REQUIRE_SIGNED_BEARER_TOKEN"] = "true"
    os.environ["MLR_REQUIRE_LIVE_GEMINI"] = "true"
    os.environ["MLR_GENIE_ENV"] = "hosted" if hosted else "local"
    if hosted:
        os.environ.setdefault("MLR_GENIE_SESSION_SECRET", secrets.token_urlsafe(48))
        os.environ.setdefault("MLR_GENIE_BOOTSTRAP_SECRET", secrets.token_urlsafe(48))
        os.environ["MLR_GENIE_ALLOWED_ORIGINS"] = ""
    import backend.main as api
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local server did not start")
            time.sleep(0.05)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", api
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def main():
    args = parse_args()
    repo = Path(__file__).resolve().parents[1]
    run = Path(tempfile.mkdtemp(prefix="mlr-live-integrations-"))
    sys.path.insert(0, str(repo))
    from backend.document_engine import create_born_digital_reference_pdf

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
import { verifyCamsSource, runCommsRxRoundtrip, MlrIntegrationError } from './camsCommsRxClient.js';
const baseUrl = process.env.MLR_INTEGRATION_BASE_URL;
const hosted = process.env.MLR_EVAL_MODE !== 'local';
const authHeaders = { 'Content-Type': 'application/json' };
if (process.env.MLR_GENIE_BOOTSTRAP_SECRET) authHeaders['X-MLR-Bootstrap-Secret'] = process.env.MLR_GENIE_BOOTSTRAP_SECRET;
async function fetchApi(path, init = {}) {
  return fetch(baseUrl + path, {signal:AbortSignal.timeout(300000),credentials:'omit',redirect:'error',...init});
}
async function issue(actor_id,role) {
  const auth = await fetchApi('/auth/token', {method:'POST', headers:authHeaders,
    body:JSON.stringify({tenant_id:'synthetic-integrations',actor_id,role})});
  if (!auth.ok) throw new Error('Token endpoint failed: ' + auth.status);
  return auth.json();
}
const health = await fetchApi('/health');
if (health.status !== 200) throw new Error('Health endpoint failed: ' + health.status);
let unauthenticatedCamsStatus = null;
if (hosted) {
  unauthenticatedCamsStatus = (await fetchApi('/integrations/cams/verify-source', {method:'POST'})).status;
  if (unauthenticatedCamsStatus !== 401) throw new Error('Unauthenticated CAMS call was not refused with 401');
}
const defaultToken = await issue('synthetic-default'); // JSON omits the undefined role.
if (defaultToken.role !== 'requester') throw new Error('Omitted token role did not resolve to requester');
const requesterToken = (await issue('synthetic-requester','requester')).access_token;
const reviewerToken = (await issue('synthetic-reviewer','reviewer')).access_token;
const options = {baseUrl, token:reviewerToken, signal:AbortSignal.timeout(300000)};
const referencePdf = new Uint8Array(await readFile(new URL('./source.pdf', import.meta.url)));
const claim = JSON.parse(await readFile(new URL('./input.json', import.meta.url), 'utf8')).claim;
const roundtripInput = {bodyText:claim, claims:[{id:'claim-external',text:claim}],
  referencePdf, requireLive:true, comments:[{commentId:'medical',claimId:'claim-external',reviewerName:'Synthetic Medical',
    reviewerRole:'Medical',timestamp:'2026-10-30T12:00:00Z',anchoredPassage:claim,commentText:'Use lowered the risk.'}],
  acceptedEdits:[{commentId:'medical',edits:[{targetText:'reduced the risk',replacementText:'lowered the risk'}]}]};
let defaultRoleAcceptanceStatus = null;
if (hosted) {
  try {
    await runCommsRxRoundtrip({...options,token:defaultToken.access_token}, roundtripInput);
  } catch (error) {
    if (!(error instanceof MlrIntegrationError) || error.status !== 403) throw error;
    defaultRoleAcceptanceStatus = error.status;
  }
  if (defaultRoleAcceptanceStatus !== 403) throw new Error('Omitted-role token was allowed to accept edits');
}
const cams = await verifyCamsSource({...options,token:requesterToken}, {claimText:claim, referencePdf, requireLive:true});
if (cams.anchor.verdict !== 'supported' || cams.anchor.page !== 2) throw new Error('CAMS anchor did not verify');
console.log('Live CAMS source verified with a requester token through TypeScript client.');
const roundtrip = await runCommsRxRoundtrip(options, roundtripInput);
const {files,...result} = roundtrip;
for (const [name,blob] of Object.entries(files)) await writeFile(new URL('./'+name,import.meta.url), new Uint8Array(await blob.arrayBuffer()));
const jobResponse = await fetchApi('/jobs/' + encodeURIComponent(result.job_id), {headers:{Authorization:'Bearer '+reviewerToken}});
if (!jobResponse.ok) throw new Error('Authenticated job lookup failed: ' + jobResponse.status);
const {job} = await jobResponse.json();
if (job.comments[0]?.human_actor !== 'synthetic-reviewer') throw new Error('Acceptance provenance lost the reviewer identity');
const acceptanceProvenance = job.metadata.accepted_comment_authorizations;
if (acceptanceProvenance?.medical?.actor !== 'synthetic-reviewer' || acceptanceProvenance.medical.role !== 'reviewer') {
  throw new Error('Acceptance authorization was not recorded for the reviewer');
}
await writeFile(new URL('./result.json',import.meta.url), JSON.stringify({cams,roundtrip:result,
  acceptance_provenance:acceptanceProvenance,
  execution:{entry_point:baseUrl,scope:process.env.MLR_EVAL_SCOPE,synthetic_fixture:true},
  authentication:{mode:hosted ? 'hosted' : 'local',bootstrap_issued:Boolean(process.env.MLR_GENIE_BOOTSTRAP_SECRET),
    evidence_role:'requester',acceptance_role:'reviewer',default_role:defaultToken.role,
    default_role_acceptance_status:defaultRoleAcceptanceStatus,unauthenticated_cams_status:unauthenticatedCamsStatus},
  health_status:health.status},null,2));
console.log('All four CommsRx artifacts downloaded through TypeScript client.');
''')
    (run / "input.json").write_text(json.dumps({"claim": claim}))
    server_context = nullcontext((args.base_url, None)) if args.base_url else local_server(run, args.hosted)
    with server_context as (base_url, api):
        mode = "hosted" if args.base_url or args.hosted else "local"
        scope = "external-https" if args.base_url else "isolated-loopback"
        env = {**os.environ, "MLR_INTEGRATION_BASE_URL": base_url, "MLR_EVAL_MODE": mode, "MLR_EVAL_SCOPE": scope}
        subprocess.run(["node", str(run / "client.mjs")], env=env, check=True, cwd=run)
        import docx
        import zipfile
        import pymupdf
        assert "lowered the risk" in "\n".join(p.text for p in docx.Document(run / "clean_revised.docx").paragraphs)
        with zipfile.ZipFile(run / "redline.docx") as redline:
            xml = redline.read("word/document.xml").decode()
            assert "<w:del " in xml and "<w:ins " in xml
            assert 'w:author="synthetic-reviewer"' in xml and 'w:author="synthetic-requester"' not in xml
        with pymupdf.open(run / "audit_summary.pdf") as audit:
            audit_text = "".join(page.get_text() for page in audit)
            assert "accepted" in audit_text and "synthetic-reviewer" in audit_text and "synthetic-requester" not in audit_text
        with pymupdf.open(run / "annotated_source.pdf") as annotated:
            assert len(annotated) == 2 and any(list(p.annots() or []) for p in annotated)
        result = json.loads((run / "result.json").read_text())
        receipts = result["cams"]["telemetry_receipts"] + result["roundtrip"]["telemetry_receipts"]
        assert receipts and all(t["used_live_sdk"] and t["transport"] in ("generate_content", "interactions") for t in receipts)
        for name, artifact in result["roundtrip"]["artifacts"].items():
            assert hashlib.sha256((run / name).read_bytes()).hexdigest() == artifact["sha256"]
        if api is not None:
            telemetry = [t.model_dump() for t in api.router.telemetry_log]
            assert telemetry and all(t["used_live_sdk"] and t["transport_mode"] == "generate_content" for t in telemetry)
            result["router_telemetry_log"] = telemetry
            result["source_sha256"] = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in [*sorted((repo / "backend").rglob("*.py")), repo / "integrations/camsCommsRxClient.ts"]}
        else:
            # Local backend sources are not evidence of the remote deployed image.
            result["client_sha256"] = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in [repo / "integrations/camsCommsRxClient.ts", Path(__file__).resolve()]}
        (run / "result.json").write_text(json.dumps(result, indent=2))
        print(f"Passed ({scope}, {mode}); {len(receipts)} live claim receipts; report: {run / 'result.json'}")


if __name__ == "__main__":
    main()
