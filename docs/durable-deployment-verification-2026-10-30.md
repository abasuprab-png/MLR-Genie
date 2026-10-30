# Durable deployment preparation — October 30, 2026

Implemented the production storage follow-up to `cd7e28a`. Deployment remains
pending: this environment has no GCP identity, selected project, controlled public
hostname, or registry manifest digest. No cloud resources, production credentials,
or existing production data were changed.

## Storage ownership

MLR Genie owns the review record, submitted evidence needed to reproduce it,
reviewer decisions, verification receipts, and generated artifacts. CAMS and
CommsRx own their source documents and workflow state and consume MLR's existing
authenticated APIs. Their integration request/response models and standalone
TypeScript client are unchanged.

The implementation uses a service-owned `mlr_genie` PostgreSQL database/schema
and private GCS bucket. Physical database infrastructure can be consolidated
later while keeping logical databases, roles, and application ownership separate.
Consumer applications do not receive MLR database or bucket credentials. No
cross-cloud shared-table dependency is introduced for Azure-hosted CAMS.

## Implementation

- Explicit database selection rejects unsupported URLs. PostgreSQL reads return
  detached records and never flush stale process caches. Unique intake creation,
  immutable tenant ownership, and revision checks prevent overwrite, resurrection,
  and lost updates. Concurrent changes return 409; callers reload before retrying.
- The private artifact manifest is stored with the matching job row and revision,
  outside public job JSON. Each HTTP request stages its authorized snapshot in
  a separate temporary directory. GCS objects have unique tenant/job keys,
  generation checks, hashes, and sizes. Uploads complete before a conditional
  database publication; failed uploads or conflicting saves leave the previous
  published set usable. Temporary files survive through response delivery and
  are then removed. Unpublished uploads remain unreferenced objects; no automatic
  deletion policy risks removing objects referenced by retained database history.
- Replacing a document or reference invalidates the downstream artifacts,
  including the integration audit PDF. Tenant reuse records are reconstructed
  from persisted jobs. Existing platform connector simulators and their in-memory
  external records/webhook deduplication remain outside this persistence change;
  job receipts themselves are persisted.
- A container image now packages the actual backend and its required evaluation
  modules. It runs as UID 10001, excludes credentials and local artifacts from its
  build context, and binds Cloud Run's `PORT` on all interfaces. Required durable
  mode refuses startup with SQLite/local artifacts. Local development and hosted
  security smoke tests can still deliberately use local storage.
- Terraform supplies Cloud SQL PostgreSQL 16 with regional HA by default,
  backups/PITR, deletion protection, IAM database login, the Cloud SQL socket,
  and bucket-scoped object permissions. It enables live Vertex execution and
  requires live model results, with the necessary Vertex and existing Veeva
  secret grants. Database passwords and IAM tokens are not placed in Terraform.

See [container instructions](../deploy/container/README.md),
[database bootstrap and recovery](../deploy/postgres/README.md), and
[Terraform configuration](../deploy/terraform/README.md). The production database
defaults reserve two vCPUs and 7.5 GiB RAM with a regional standby; capacity and
cost need to be included in the deployment plan review.

## Verification

The complete Python suite passed **288 tests** with an isolated real
**PostgreSQL 16.15** server. Both `MLR_GENIE_TEST_POSTGRES_URL` and
`MLR_TEST_POSTGRES_DSN` were supplied securely to `pytest -v`; Gemini credentials
were disabled for these tests. Relevant coverage includes:

- Independent database stores observe committed changes without writing on GET
  or count; stale saves conflict; deleted rows do not reappear; concurrent intake
  creates one row; tenant ownership and private manifest separation hold.
- Eight API persistence cases use real PostgreSQL with an explicitly injected GCS
  SDK double. A fresh API/store/workspace downloads all four artifacts with matching
  hashes and reviewer provenance. Cross-tenant and opaque job IDs remain isolated.
  Partial uploads and post-upload database conflicts preserve the prior manifest.
  Interleaved reads return a consistent job/artifact snapshot. A separate startup
  case refuses required durable mode with local storage.
- Twenty-five focused artifact cases cover all seven numbered files, immutable
  generations, stale workspace rejection, checksum/size verification, rejected
  forged manifests, upload failure, and response-lifetime cleanup.

The temporary PostgreSQL container and its credentials were removed after testing.
Database-dependent tests intentionally skip when their test URLs are absent;
ordinary test runs without PostgreSQL do not establish the shared-store results.

Both TypeScript checks, **32 TypeScript tests**, and the frontend build passed.
Terraform format, validation, and dependency checks passed with provider 6.50.0.
The SQL bootstrap was exercised on a separate PostgreSQL 16 container: the
IAM-shaped role owns the schema and can create/write its tables, repeated
bootstrap succeeds, and missing-role/wrong-database inputs fail. Cloud IAM token
refresh was checked using mocked credentials, not a Cloud SQL service.

The rebuilt container passed health 200, tokenless CAMS 401, nonroot execution,
source/context exclusion checks, missing-signing startup refusal, and durable-mode
refusal with local storage. Its runtime source hashes match the checked source.
The local image ID is
`sha256:a68375745e993599ceb031f3c77a711384402a1222ae7ef983652869865e321a`.
This is **not a registry manifest digest**. See
[container evidence](durable-container-verification-2026-10-30.json).

The hosted live evaluator passed again using real loopback HTTP, the compiled
TypeScript client, temporary SQLite/local artifacts, and unmocked Gemini:
tokenless CAMS 401, omitted-role acceptance 403, requester CAMS success, and
reviewer roundtrip with all four artifacts verified. Its seven live model calls
and two claim receipts do not establish Cloud SQL/GCS or Vertex authentication.
See [hosted live evidence](durable-hosted-live-verification-2026-10-30.json).

A fresh independent read-only review found no concrete new bugs in the requested
persistence, isolation, publication, startup, bootstrap, or packaging boundaries.
Existing Starlette/Vite deprecation warnings remain.

## Remaining deployment work

Configure the intended GCP identity, project, region, and controlled hostname.
Push the checked image to the selected registry and record its manifest digest.
Review the Terraform plan, provision the dependency resources, and bootstrap the
database schema before starting the first revision. Supply production secret
versions securely and verify actual model availability in the chosen region.

If an earlier deployment has real SQLite data or local files, preserve and
migrate them with reconciliation before switching traffic; this change does not
automatically import or discard them. Test actual Cloud SQL IAM login and token
refresh, GCS permissions, uploads on one revision and downloads on another,
revision replacement, backup/restore, public DNS/certificate readiness, external
health/auth checks, and the hosted evaluator through the public URL. Record one
requester verification using a real journal PDF and a reworded claim. Actual
Cloud Armor limits and Azure connectivity also require the deployed service.
