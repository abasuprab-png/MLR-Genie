# Live SDK verification and red-team results — October 30, 2026

The live SDK transport works after the changes in this revision. The five-point
product check is **not a clean pass**: accepted conflicting comments are not
applied, and a paraphrased patient adverse event is not placed on hold. Label and
comment tools also fail to supply their actual inputs to Gemini and ignore its
responses. No clinical keyword lists were added to make these checks pass.

## Method and initial result

The initial run used the unmodified `ec6a082ab5d74260adccd15c223dc2e2c064159e`
checkout, the environment's `GEMINI_API_KEY`, a real Uvicorn server, real HTTP
requests, and an actual `google.genai.Client`. There were no model/SDK mocks.
All clinical content and identities were synthetic. Python return-value
observation recorded model responses without replacing calls or outputs.

The sequence was `/auth/token`, `/jobs/intake`, working DOCX upload, reference PDF
upload, conflicting `/comments`, `/diff-label`, acceptance of one comment and
rejection of the other, then `/execute-file-roundtrip`.

The initial run returned HTTP 200 from these routes but recorded **nine
`provider_error` generation results**. Google returned 404 for the mapped
`gemini-2.5-pro` and `gemini-2.5-flash-lite` IDs, stating they were unavailable to
new users on this account. A successful application HTTP response did not mean a
successful model call.

The authenticated model catalog listed `gemini-3.1-pro-preview`,
`gemini-3.8-flash`, `gemini-4-argon`, and `gemini-3.5-flash-lite`.
`gemini-4-argon` did not advertise `createCachedContent`; the Pro preview model
did. Deep review now uses `gemini-3.1-pro-preview`, orchestration resolves to
`gemini-3.8-flash`, and PV gating resolves to `gemini-3.5-flash-lite`.
Orchestration and embeddings were not invoked by this end-to-end workflow.

A subsequent live request isolated another error: Google returned 400 when
`cached_content` and `system_instruction` were both supplied to generation.
The previous router silently retried without the cache. The corrected request
places the per-turn instruction in the input and keeps the cache's system
instruction, so PDF-backed verification retains its reference context.
No fenced-JSON or safety-field validation errors occurred in the executed calls.

## Final transport evidence

The final full workflow completed with **13/13 live generation calls**, zero
`provider_error` results, a successfully created live reference cache, and six
JSON responses that validated as `ClaimVerificationVerdict` objects. All
generation telemetry entries recorded `used_live_sdk: true` and
`transport_mode: generate_content`. PDF-backed generation retained its cache.
A supplemental population-shift request uploaded the same PDF to the shifted
job and verified `contradicted` with a live, cached Gemini call: **14/14 successful
generation calls in the final workflow plus that supplemental check**.

`ModelCallTelemetry.model` still records the logical role. Its new
`effective_model` field and the job's claim receipts record the SDK model ID.
Representative final job receipt:

```json
{
  "used_live_sdk": true,
  "effective_model": "gemini-3.1-pro-preview",
  "transport": "generate_content"
}
```

| Task | Actual model | Transport | Live |
| --- | --- | --- | --- |
| Claim verification, comments, label diff | `gemini-3.1-pro-preview` | `generate_content` | `true` |
| PV screening | `gemini-3.5-flash-lite` | `generate_content` | `true` |

Live transport success establishes that requests reached the provider; it does
not establish that a caller supplied sufficient context or used the response.
The returned retention and service-tier configuration flags were not independent
provider attestations and are not relied on here.

## Five-point outcomes

| Check | Observed result | Assessment |
| --- | --- | --- |
| Supported claim in a two-page, two-column PDF | `supported`; valid box `[2, 54.0, 96.0, 285.25, 137.12]`. Independent PyMuPDF extraction from that box matched the reference quote. | Pass |
| Population shifted from adults to children | Application classification `contradicted`, not `supported`. | Pass for the required classification; supplemental PDF-backed evidence is recorded in the JSON report. |
| Section 12.1 MoA hedge plus Section 14 change from 38% to 35% | `fits_prepared_scenario: false`; launch asset flagged for both changes. | API flags correct, but **not a live semantic pass**: Gemini asked for the label, scenarios, and assets because the prompt omitted them. |
| Conflicting Medical/Regulatory comments, then accept one edit | Conflict detection returned `requires_human_resolution: true`. Submitting one accepted and one rejected comment reset **both** to `escalated`. | **Fail:** the accepted edit was not applied. |
| Paraphrased patient adverse event | Gemini identified an individual patient, suspect drug, adverse event, and need for safety review. The API returned `cv08_flagged: false`, state `draft`. | **Fail:** the job was not placed on hold. |

The fourth check did generate all four required files with nonzero sizes:
`04_revised_clean_v2.docx`, `05_tracked_changes_redline_v1_to_v2.docx`,
`03_annotated_reference.pdf`, and `06_resubmission_package.zip`. The annotated PDF
contained one annotation and the ZIP passed its integrity check. However, the
clean document did not contain the accepted edit, and the redline XML contained
neither `<w:del>` nor `<w:ins>`. File existence alone did not pass the workflow.

The synthetic adverse-event narrative was:

> My aunt became so light-headed that she crumpled to the kitchen floor after
> swallowing her first Aurenix tablet; paramedics took her straight to hospital.

`screen_pharmacovigilance_cv08` discards the model return value and bases `flagged`
on its existing marker/regex checks. `import_and_reconcile_comments` also
discards the model response, submits only a comment count/job ID, and escalates
overlapping comments without respecting the subsequent accepted/rejected
decisions. `analyze_approval_day_label_delta` sends only indication/scenario-count
information and discards the response. These are existing decision-wiring
defects; they were recorded without changing clinical decision rules.

## Bootstrap-secret protection and regression tests

`POST /auth/token` now reads `MLR_GENIE_BOOTSTRAP_SECRET` for each request and
requires the matching `X-MLR-Bootstrap-Secret` header when the variable has a
nonempty value. A real HTTP check with an isolated, temporary process secret
confirmed missing header → 401, incorrect header → 403, correct header → 200
and successful use of the resulting signed token in the live workflow.
The configured environment's bootstrap secret remains unset; set it securely
to enable this protection in a hosted deployment.

The full offline `pytest -v` regression run passed **89 tests**. This includes
the existing checks plus bootstrap-secret tests, updated SDK model expectations,
actual-model telemetry checks, and cached-request configuration/retry checks.
Those regression results are separate from the live red-team failures above.

See [the sanitized machine-readable evidence](live-verification-2026-10-30.json)
for endpoint statuses, telemetry, artifact checks, and selected model responses.
