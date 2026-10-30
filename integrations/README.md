# CAMS and CommsRx integration surface

This prepares MLR Genie for the Stage 3 CAMS source-reference fields described
for `5c06478` and the Stage 4 CommsRx Review Room. It does not modify either
consumer repository. Copy `camsCommsRxClient.ts` into either application; the
module has no imports or package dependencies and uses standard Fetch, FormData,
Blob, and AbortSignal APIs (browsers and Node 18+).

## Authentication and transport

Use an already-issued MLR signed session token. Both integration endpoints use
the existing tenant/actor authentication checks; the client always sends
`Authorization: Bearer <token>`, including on artifact downloads. Tenant and actor
are derived from authentication, never from caller-supplied job identity.
Set `MLR_REQUIRE_SIGNED_BEARER_TOKEN=true` for hosted deployments. Obtain tokens
through your trusted server; keep `MLR_GENIE_BOOTSTRAP_SECRET` out of browser code.

Both POST endpoints accept multipart form data:

| Field | Value |
| --- | --- |
| `payload` | JSON string matching the input type below |
| `reference_pdf` | Readable, unencrypted PDF, maximum 32 MiB |

The client accepts PDF input as a `Blob`, `ArrayBuffer`, or `Uint8Array`. It sets
the multipart boundary automatically. Source metadata is optional:
`{ sourceId?, version?, title?, url? }`. URLs are metadata only; the server does
not fetch them. The uploaded PDF is the source of evidence.

Each POST creates a fresh MLR job. The client does not retry mutations. An
`AbortSignal` cancels waiting but does not guarantee cancellation of server work.

## CAMS: verify one source for one claim

`POST /integrations/cams/verify-source`

```ts
import { verifyCamsSource } from "./camsCommsRxClient";

const result = await verifyCamsSource(
  { baseUrl: "https://mlr.example/api", token: signedSessionToken },
  {
    claimText: claim.text,
    referencePdf: selectedPdf,
    source: { sourceId: reference.id, title: reference.title, version: "1" },
    requireLive: true,
  },
);

// Persist on this source reference, alongside result.job_id and result.claim_id.
// Replace the old verification fields; clear old page/box/quote if now absent.
const verification = result.anchor;
```

Successful response:

```json
{
  "job_id": "integration_...",
  "claim_id": "clm_integration_..._1",
  "anchor": {
    "page": 2,
    "boundingBox": { "x": 54, "y": 96, "width": 231.25, "height": 41.12 },
    "verdict": "supported",
    "verifiedBy": "gemini-3.1-pro-preview+pymupdf",
    "verifiedAt": "2026-10-30T17:00:00Z",
    "verbatimQuote": "Exact text extracted from the verified PDF rectangle."
  },
  "coordinateSpace": "pdf_points",
  "pageDimensions": { "width": 612, "height": 792 },
  "telemetry_receipts": [
    {
      "claim_id": "clm_integration_..._1",
      "used_live_sdk": true,
      "effective_model": "gemini-3.1-pro-preview",
      "transport": "generate_content"
    }
  ]
}
```

`page` is one-based. `boundingBox` uses PDF points (1/72 inch) with a top-left
origin, not CSS pixels or normalized coordinates. Scale using `pageDimensions`
and the viewer viewport. `verbatimQuote` preserves the extracted PDF line breaks.
The service checks that the rectangle is inside the page and that its extracted
text matches the anchor quote before returning coordinates.

| MLR classification | CAMS verdict |
| --- | --- |
| `supported`, with a validated PDF anchor | `supported` |
| `contradicted` | `contradicted` |
| `partially_supported`, `unresolved`, or supported without a validated anchor | `insufficient` |

For missing/textless evidence, `page`, `boundingBox`, `verbatimQuote`, and
`pageDimensions` are omitted. This endpoint does not invent OCR coordinates.
One submitted claim remains one MLR claim, including multi-sentence claims.
An anchor locates the compared passage; its presence alone does not imply support.

## CommsRx: accepted edits to review artifacts

`POST /integrations/commsrx/roundtrip`

```ts
import { runCommsRxRoundtrip } from "./camsCommsRxClient";

const result = await runCommsRxRoundtrip(
  { baseUrl: "https://mlr.example/api", token: signedSessionToken },
  {
    title: "Review Room round 2",
    bodyText: "Signal stability improved by 20% in adults.",
    claims: [{ id: "claim-1", text: "Signal stability improved by 20% in adults." }],
    referencePdf: selectedPdf,
    requireLive: true,
    comments: [{
      commentId: "medical-1", claimId: "claim-1",
      reviewerName: "Medical reviewer", reviewerRole: "Medical",
      timestamp: "2026-10-30T12:00:00Z",
      anchoredPassage: "Signal stability improved by 20% in adults.",
      commentText: "Use increased rather than improved.",
    }],
    acceptedEdits: [{
      commentId: "medical-1",
      edits: [{ targetText: "improved", replacementText: "increased" }],
    }],
  },
);

// Browser: create an object URL and download using the matching filename.
const redline = result.files["redline.docx"];
// Server: await redline.arrayBuffer() to store the bytes.
```

Claims and anchored comment passages must exist verbatim in `bodyText`; IDs
must be unique and comment/accepted-edit references must resolve. `acceptedEdits`
records an explicit human decision on an existing comment. Alternatively, send
`proposedEdits` and `decision: "accepted"` on the comment itself. The authenticated
actor owns the acceptance. Supported reviewer roles are Medical, Legal,
Regulatory, and MLR Operations. `sector` defaults to `corporate`; supply the
appropriate sector for product-specific review requirements.

The service preserves the submitted plain text as the original DOCX, uses the
uploaded reference PDF, reconciles comments, and invokes the existing roundtrip
pipeline. This surface accepts text plus PDF; it does not import an existing
formatted DOCX. The existing job upload endpoints remain available for that flow.

The JSON response contains `job_id`, `state`, `requires_human_resolution: false`,
`claims`, `telemetry_receipts`, and exactly these artifact descriptors:

| Response key / download filename | Contents |
| --- | --- |
| `redline.docx` | Existing tracked-change DOCX with accepted deletions/insertions |
| `clean_revised.docx` | Accepted edits in a clean revised DOCX |
| `audit_summary.pdf` | Separate paginated claim evidence, reviewer decisions, provenance, and transport report |
| `annotated_source.pdf` | Uploaded reference PDF annotated with evidence anchors |

Each descriptor contains `filename`, `download_url`, `media_type`, `bytes`, and
`sha256`. Canonical download URLs are
`/integrations/commsrx/jobs/{job_id}/artifacts/{filename}` and enforce tenant
authentication. The client validates descriptors, downloads all four artifacts,
checks size and media type, and returns Blobs in `result.files`. It rejects
foreign or unexpected artifact URLs. SHA-256 values are available for additional
integrity checks. The original numbered job artifacts and ZIP remain unchanged.

## Errors and verification modes

`MlrIntegrationError` exposes `status` and the parsed response in `detail`.
It uses `status: 0` for a malformed response or invalid artifact download.

| HTTP status | Meaning |
| --- | --- |
| 400 / 413 | Invalid/empty/encrypted PDF or excessive file size |
| 401 / 403 | Missing/invalid credentials or cross-tenant access |
| 409 | CV-08 hold, unresolved reviewer conflict, or an accepted edit could not be placed uniquely |
| 422 | Invalid payload, claim span, duplicate ID, or unknown claim/comment reference |
| 502 | Required live assessment unavailable or invalid |

For 409, `detail.job_id` identifies the persisted review job; conflicts include
`requires_human_resolution` and conflict groups, and unplaced edits include
`unplaced_comment_ids`. No successful integration artifact response is returned.
Use existing job/comment endpoints for subsequent human review.

With configured Gemini credentials, assessments use the live SDK and provider
failure cannot silently yield local-only success. `requireLive: true` additionally
requires live operation when no provider is configured. Explicit offline operation
is supported for development, reported as `used_live_sdk: false` and
`verifiedBy: "mlr-genie:offline+pymupdf"`; it is not a live verification receipt.

## Tests

- Backend: `pytest -v` includes real PyMuPDF/DOCX contract checks, verdict mapping,
  omitted coordinates for textless PDFs, authentication, persistence, audit
  content, accepted edits, and four authenticated artifact downloads.
- TypeScript: `npm test`; `npm run check`; `npm run client:check`.
- Opt-in live integration: `.venv/bin/python evals/live_integrations.py`. This
  compiles the standalone client, runs it with Node against a temporary Uvicorn
  server and database, invokes unmocked Gemini, downloads all four files, verifies
  hashes and document contents, and saves a report in `/tmp/mlr-live-integrations-*`.

On October 30, 2026, the full suite passed 128 Python tests and 32 TypeScript
tests; both TypeScript checks passed. The compiled-client live smoke test passed
with seven unmocked Gemini calls, `used_live_sdk: true` throughout, and all four
artifacts downloaded and verified. See the
[sanitized integration evidence](../docs/integration-verification-2026-10-30.json).

These contracts and checks prepare the consumer integration. They do not claim
that CAMS or CommsRx production UI wiring has already been deployed.
