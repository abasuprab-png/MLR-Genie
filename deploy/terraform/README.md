# External HTTPS entry point

The global external Application Load Balancer forwards HTTPS through a regional
serverless NEG to Cloud Run. Keep `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`:
Azure callers use the public load balancer, while direct Internet access to the
Cloud Run URL remains blocked. The service sets `invoker_iam_disabled=true`
because the load balancer forwards MLR bearer tokens rather than Google identity
tokens. Hosted application authentication remains mandatory. This service-scoped
setting follows Google's [public-access guidance](https://docs.cloud.google.com/run/docs/authenticating/public)
for projects with domain-restricted sharing, without an `allUsers` IAM grant or
an organization-policy change.

Cloud Armor throttles `/auth/*` at 60 requests per minute per source IP and
`/integrations/*` at 600. Excess requests receive 429 before application work.
These limits aggregate callers behind the same Azure outbound IP; assess their
capacity before rollout. Cloud Armor limits are approximate traffic controls,
not a token-issuance authorization mechanism.

The hosted revision enables both Vertex selection and live Vertex client
creation (`GOOGLE_GENAI_USE_VERTEXAI=true` and `GOOGLE_GENAI_LIVE_VERTEX=true`).
`MLR_REQUIRE_LIVE_GEMINI=true` prevents model-dependent routes from returning
offline results when credentials, model availability, or provider calls fail.
Terraform enables the Vertex AI API and grants the runtime service account
`roles/aiplatform.user` in the selected project before creating the revision.
`vertex_ai_location` controls inference separately from the deployment/storage
`region`; its default preserves the region. Global AI processing is rejected by
default and requires both `vertex_ai_location=global` and the explicit boolean
`allow_global_inference=true`. A Cloud Run lifecycle precondition blocks an
unapproved global configuration, independently of the deployment driver's guard.

The candidate target in this branch proposes global inference because the
configured deep model (`gemini-3.1-pro-preview`) was not available through the US
endpoint. Global processing has **no US-jurisdiction ML processing guarantee**:
document and prompt contents sent to Gemini may be processed outside the US.
Cloud Run, Cloud SQL, GCS artifacts, and secrets remain in `us-central1`; their
storage location does not constrain the AI processing location. Model IDs,
application authentication, and integration API shapes are unchanged.

This candidate is a reviewable proposal, not deployment evidence. It must remain
undeployed until the user explicitly approves the global processing choice. The
synthetic runtime probe receives `MLR_VERTEX_PROBE_ALLOW_GLOBAL=true` only for an
explicitly approved global target; live tests must still pass before release is
reported successful.
Veeva credentials are mounted only when `mount_veeva_credentials=true`; that
optional mount has its own secret-scoped access grant and requires a real secret
version before enabling it. CAMS and CommsRx do not require Veeva credentials.

The container defaults to 2 GiB memory and four concurrent requests per instance
for PDF processing and temporary artifact staging. These settings are configurable
with `runtime_memory` and `runtime_request_concurrency`.

## Durable storage configuration

The hosted revision is configured to use PostgreSQL 16 for job JSON, including
`job.receipts`, and private artifact manifests, with IAM database authentication
through Cloud Run's managed Cloud SQL Auth Proxy socket. The default instance
uses regional high availability,
automated backups, point-in-time recovery, and deletion protection. Database
capacity, availability, and protection settings are configurable. The application
is required to use durable storage (`MLR_GENIE_REQUIRE_DURABLE_STORAGE=true`);
database failures must not silently fall back to local SQLite.

Artifacts use the GCS backend and the private, versioned bucket. The runtime
receives object access on that bucket only; artifact downloads still pass through
application tenant authentication. Cloud Run instances share the authoritative
database and object storage rather than relying on instance-local files.

Before starting the first revision, an authorized database administrator must
create the administrator-owned schema and grant the runtime IAM role `USAGE`
and `CREATE`. The runtime then owns the tables it creates. See
[PostgreSQL bootstrap, migration, and recovery operations](../postgres/README.md).
Schema bootstrap is separate from Cloud IAM and requires a staged first rollout.
Real Cloud SQL IAM refresh, cross-revision persistence, GCS authorization, and
backup/restore verification remain required cloud deployment checks.

The external connector adapters' `external_store` and webhook deduplication
remain process-local simulators. Their state is outside this CAMS/CommsRx durable
storage path; this configuration does not claim standalone connector idempotency
records are persisted in PostgreSQL.

## Validation and deployment prerequisites

Validate without deploying:

```sh
terraform init -backend=false
terraform fmt -check
terraform validate
```

Deployment is a separate authorized step. Before it, supply `project_id`,
`region`, an immutable `app_image` digest, and a controlled DNS `public_host`
(for example, `mlr.example.org`). Enable the Compute Engine, Cloud Run, Secret
Manager, and required image/provider APIs in that project. The deployment actor
needs the corresponding resource-management and service-account permissions,
including permission to update Cloud Run's service-level invoker setting. An
enforced `constraints/run.managed.requireInvokerIam` policy would prevent this
configuration and must be reviewed separately; domain-restricted sharing alone
does not require a policy exception. Populate the
signing and bootstrap Secret Manager versions outside Terraform before starting
the revision. Supply Veeva credentials only if its optional mount
is enabled. No secret values belong in plans or state.
Complete the PostgreSQL schema bootstrap before the first revision. Existing
SQLite jobs and local artifacts are not migrated merely by changing environment
variables; follow the migration checks in the database operations guide.

Start from [terraform.tfvars.example](terraform.tfvars.example), replacing every
project, region, hostname, and image placeholder with approved values. The
example contains no working deployment target or credentials; local
`terraform.tfvars` files are ignored by Git.

After an authorized apply, create the `public_host` DNS A record using
`terraform output -raw public_ip`. Remove conflicting AAAA records unless an
IPv6 frontend is separately configured. Wait for the Google-managed certificate
to become `ACTIVE`; Terraform resource creation alone does not establish HTTPS
readiness. The service URL is `terraform output -raw public_url`. Configure CAMS
on Azure and CommsRx with that HTTPS URL, without a path prefix.

From outside Google Cloud, verify `/health` returns 200 and a tokenless multipart
POST to `/integrations/cams/verify-source` returns 401. Then run the hosted live
integration evaluator through the public URL. Record the host, deployed image
digest, and a successful requester verification using a real journal PDF with a
reworded claim in the authorized deployment's evidence. A local hosted-mode test
does not prove public DNS, certificate, load-balancer, or Azure connectivity.

The [real-journal verification guide](../../docs/real-journal-verification-2026-10-30.md)
provides a checked publisher PDF, reworded claim, source hash, and a reusable
`evals/live_journal_verification.py --base-url https://<host>` command for that
final requester test. Its existing local evidence is explicitly separate from
production verification.

For the authorized `commsrx-ai` deployment, the keyless release workflow and
the private GCS Terraform backend are documented in
[deployment automation](../automation/README.md). The deployment driver reports
the load-balancer IP before waiting for external DNS and the managed certificate.
