# Live SDK verification and red-team results — October 30, 2026

**All five live red-team checks pass after fixing the application wiring gaps
reported at `1fae0cc`.** The final run made **19/19 successful, unmocked Gemini
generation calls**, with zero `provider_error` results. Every generation receipt
records `used_live_sdk: true` and `transport_mode: generate_content`.
No clinical keyword lists or scenario-specific application rules were added.

## Five-point results

| Check | Live observed result | Result |
| --- | --- | --- |
| Supported clinical claim in a two-page, two-column PDF | `supported`; PyMuPDF box `[2, 54.0, 96.0, 285.25, 137.12]` is within the page and independent clipped extraction matches the source quote. | **Pass** |
| Population shifted from adults to children | `contradicted` after uploading the same PDF to the shifted job; live generation uses the reference context. | **Pass** |
| Approval-Day Section 12.1 hedge and Section 14 clinical figure update | `fits_prepared_scenario: false`; Gemini flags both affected claims in `launch_brief` and proposes the qualified mechanism wording and replacement of 38% with 35%. All model label quotes are verified verbatim in code. | **Pass** |
| Conflicting Medical/Regulatory comments, then human acceptance | Initially `requires_human_resolution: true`; accepting Medical and rejecting Regulatory produces `false`, preserves those decisions, and applies “lowered the risk.” The clean DOCX matches the job revision; redline XML contains real `<w:del>` and `<w:ins>` elements. | **Pass** |
| Paraphrased individual adverse event | `cv08_flagged: true`, state `hold`; verified patient, product, and event spans are stored as a structured safety finding. | **Pass** |

The ordinary intake also enters `in_review` with extracted claims and safety
findings. The adverse-event intake enters the stricter CV-08 `hold` before
creating communications revisions. Its narrative was unchanged from the
previous failing check:

> My aunt became so light-headed that she crumpled to the kitchen floor after
> swallowing her first Aurenix tablet; paramedics took her straight to hospital.

## What changed

**Label assessment:** The existing deterministic section/text diff remains
Layer 1. `GeminiGateway` sends the changed sections, prepared scenarios, and
asset claims in one structured-schema call. The application validates complete
section-by-claim coverage, asset references, and verbatim approved-label quotes
before merging model findings with the deterministic result. Proposed wording
requires human approval; it is not automatically applied to assets.

The live model returned six section/claim assessments, including two revisions:

- Section 12.1: “Aurenix is thought to reduce receptor signaling; the mechanism in
  humans has not been fully established.”
- Section 14: the efficacy claim is revised to 35%, citing the supplied approved
  value `35%` verbatim.

The response includes the full `model_assessment`, `used_live_sdk`, verified-quote
status, and telemetry. Configured live mode, `MLR_REQUIRE_LIVE_GEMINI=true`, or
`require_live: true` on `/diff-label` prohibits silent local fallback. Provider
failure, empty/invalid output, unknown citations, or incomplete coverage returns
HTTP 502 with `assessment_failed: true`.

**Human resolution:** Decisions record the versions of known comments on the
target and the authenticated human actor. Reimporting unchanged comments or
reloading storage preserves the resolution. A new or changed conflicting
comment reopens review; simultaneous incompatible acceptances remain unresolved.
The accepted edit reaches the existing atomic edit and OOXML generation paths.

**Claim and safety extraction:** The complete submitted copy reaches Gemini
under a structured schema. Application code checks every cited span, including
patient/product/event evidence, before storing findings. Findings have source
hashes and offsets, merge without duplication, and update job state. Model
claims and safety statements enter claim verification even when the previous
paragraph filter would skip them. Claim verification now receives authorized
source passages explicitly, including when there is no usable cache.

## Live transport and artifacts

Final run: `run-a8c4a0ad76`, using a real Uvicorn server, real HTTP requests, the
environment API key, and `google.genai.Client`. All documents and identities were
synthetic. No model calls, responses, or SDK methods were mocked or replaced.
The test observed returned responses without altering execution.

| Task | Calls | Effective model | Transport | `used_live_sdk` |
| --- | ---: | --- | --- | --- |
| Claim verification | 12 | `gemini-3.1-pro-preview` | `generate_content` | `true` |
| Comment reconciliation | 2 | `gemini-3.1-pro-preview` | `generate_content` | `true` |
| Approval-Day label diff | 1 | `gemini-3.1-pro-preview` | `generate_content` | `true` |
| Claim/safety extraction and CV-08 screening | 4 | `gemini-3.5-flash-lite` | `generate_content` | `true` |

All 12 claim responses validated as `ClaimVerificationVerdict`. PDF-backed calls
retained live reference caches. Logical role IDs remain separate from actual SDK
model IDs; orchestration and embeddings were not exercised by this workflow.

The job persists claim receipts in `metadata.telemetry_receipts` and safety,
comment, and label receipts in `metadata.assessment_telemetry_receipts`.
A representative final claim receipt is:

```json
{
  "used_live_sdk": true,
  "effective_model": "gemini-3.1-pro-preview",
  "transport": "generate_content",
  "verification_mode": "live_sdk_execution"
}
```

`execute-file-roundtrip` produced and validated:

- `04_revised_clean_v2.docx`: contains “lowered the risk” and matches the revised
  job body; the original DOCX retains “reduced the risk.”
- `05_tracked_changes_redline_v1_to_v2.docx`: `word/document.xml` deletes “reduced
  the risk” and inserts “lowered the risk.”
- `03_annotated_reference.pdf`: contains reference annotations.
- `06_resubmission_package.zip`: passes ZIP integrity validation.

The HTTP sequence covered token issuance, intake, DOCX upload, PDF upload,
conflicting comments, label diff, accepting/rejecting comments, and file
roundtrip, followed by the shifted-population PDF workflow and adverse-event
intake. Every endpoint returned HTTP 200. A temporary process-local bootstrap
secret also verified missing header → 401, wrong header → 403, and correct header
→ 200. This did not change the environment's hosted bootstrap configuration.

## Regression verification and reproduction

The complete `pytest -v` suite passed **116 tests**: all previous 89 tests plus
27 application regression cases. These cover invalid citations, incomplete
label coverage, provider failure, empty model responses, persistence, duplicate
findings, active-review state, accepted edits, authentic OOXML changes, and
reopened conflicts. The run used an isolated copy of the unchanged source to
avoid overwriting tracked demo artifacts. One existing Starlette test-client
deprecation warning remains.

`npm test` passed **11 tests**; `npm run check` and `npm run client:check` both
passed. Existing Vite plugin deprecation warnings remain.

To repeat the unmocked check with `GEMINI_API_KEY` supplied securely:

```bash
.venv/bin/python evals/live_red_team.py --bootstrap-probe
```

The script uses a temporary SQLite database and a random local port, creates
synthetic input files, and exits unsuccessfully if any endpoint, provider call,
citation, artifact, or red-team assertion fails. Reports default to a temporary
`run-*` directory; set `MLR_LIVE_REPORT_DIR` to choose another report parent.
Generated job artifacts remain in the ignored `artifacts/file_roundtrip` tree.

[The sanitized JSON evidence](live-verification-2026-10-30.json) contains all 19
router receipts, persisted job receipts, endpoint statuses, selected structured
assessments, and SHA-256 hashes of the exact backend files verified. The base
commit is `1fae0cc`; the hashes identify the application changes tested before
this report was committed. No credentials or signed session tokens are included.

The original verification at `ec6a082` exposed unavailable 2.5 model mappings and
a cached-request system-instruction error; those transport fixes were already
in `1fae0cc`. This follow-up verifies that successful model responses now affect
application behavior. These five synthetic checks establish the observed cases,
not general clinical accuracy or provider retention guarantees.
