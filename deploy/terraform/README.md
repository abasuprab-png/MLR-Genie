# External HTTPS entry point

The global external Application Load Balancer forwards HTTPS through a regional
serverless NEG to Cloud Run. Keep `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`:
Azure callers use the public load balancer, while direct Internet access to the
Cloud Run URL remains blocked. `allUsers` receives Cloud Run invoker permission
because the load balancer forwards MLR bearer tokens rather than Google identity
tokens. Hosted application authentication remains mandatory.

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
The existing Veeva environment-secret reference also has its own secret-scoped
access grant. Its secret version remains a deployment prerequisite.

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
create the service-owned schema and grant it to the runtime IAM role. See
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
needs the corresponding resource-management and service-account permissions;
an organization policy that forbids `allUsers` invocation must be resolved for
this application-authenticated architecture before rollout. Populate the
existing signing, bootstrap, and Veeva Secret Manager versions outside Terraform
before starting the revision. No secret values belong in plans or state.
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
