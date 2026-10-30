# Keyless deployment access

Run `bootstrap-github-oidc.sh` in an already signed-in Google Cloud Shell for the
authorized project **`commsrx-ai` (`333570013714`)**. It does not sign in,
read credential files, create service-account keys, or print access tokens. It
checks both the project ID and immutable project number before changing anything.

The script creates a dedicated deployment service account and a GitHub Workload
Identity Federation provider. Trust is restricted to the repository's immutable
ID `1439565611`, owner ID `227769156`, and **`refs/heads/deploy/production`**.
Only `push` and `workflow_dispatch` events are accepted. Opening a pull request,
including a `pull_request_target` event, or pushing another branch cannot use
this provider.
Existing identities without the script's ownership marker cause a stop rather
than being adopted. Unexpected providers in the pool also cause a stop. Disabled
identities are not re-enabled automatically.

Review the script and its dry run first:

```sh
bash deploy/automation/bootstrap-github-oidc.sh --dry-run
```

For the one-time setup, use a reviewed commit, not a moving branch:

```sh
(
  set -e
  mlr_bootstrap="$(mktemp)"
  trap 'rm -f "$mlr_bootstrap"' EXIT
  curl -fsSL 'https://raw.githubusercontent.com/abasuprab-png/MLR-Genie/<reviewed-commit>/deploy/automation/bootstrap-github-oidc.sh' \
    -o "$mlr_bootstrap"
  bash "$mlr_bootstrap"
)
```

Replace `<reviewed-commit>` with the exact commit supplied with the deployment
handoff. The command stops if downloading fails, and removes its unique temporary
file afterward. The existing Cloud Shell
account needs permission to enable APIs, administer workload identity/service
accounts, and grant the listed project IAM roles. A permission failure stops the
script; it does not request a new login or change the selected global project.

The dedicated deployment account receives these project-scoped roles, which are
needed to manage the Terraform resources and their runtime access:

- `roles/serviceusage.serviceUsageAdmin`
- `roles/iam.serviceAccountAdmin`
- `roles/resourcemanager.projectIamAdmin`
- `roles/compute.admin`
- `roles/run.admin`
- `roles/storage.admin`
- `roles/secretmanager.admin`
- `roles/cloudsql.admin`
- `roles/artifactregistry.admin`

These are powerful deployment permissions within this project. No Owner,
Editor, organization-level role, or project-wide service-account impersonation
grant is added. The deployment driver must grant `serviceAccountUser` only on
the application's specific runtime service account after creating it.

This bootstrap establishes access; it does not provision the paid database,
deploy the application, change DNS, or route traffic. IAM propagation can take a
few minutes. Its final output contains only non-secret deployment identifiers and
can be shared to confirm readiness. A controlled hostname is still needed for
the managed HTTPS certificate. The deployment and verification stages run next
from the deliberately updated release branch.

## Production release and verification

`target.json` selects project `commsrx-ai` (`333570013714`), region `us-central1`,
and the previously unused hostname `mlr-genie.signalfidelitygroup.com`. DNS for
that domain is authoritative at Cloudflare. A DNS-only A record must point the
hostname at the load-balancer IP; writing a record into a non-authoritative Vercel
zone would not publish it.

The deployment workflow runs only for the designated `deploy/production` branch.
After the setup is confirmed, advance that branch to the reviewed master commit.
The workflow checks Python/PostgreSQL, client tests and types, and Terraform before
requesting short-lived Google credentials. The driver verifies the project ID,
number, identity, and source commit; it rejects resource deletion/replacement
plans, uses a private versioned GCS state bucket, and records a sanitized plan
summary. It builds and pushes an immutable image, provisions the database and
scoped runtime access, creates absent signing/bootstrap secrets privately, and
bootstraps the SQL schema through a temporary narrowly scoped import grant.
It never resets an administrator password or substitutes fake Veeva credentials.

The target separates the application's GCP `region` from `vertex_ai_location`.
The candidate in this branch proposes `vertex_ai_location=global` with
`allow_global_inference=true` because the configured deep model was not available
through the US endpoint. It is a reviewable proposal awaiting an explicit user
choice; it has not been deployed. Global Gemini processing does **not** provide a
US-jurisdiction ML processing guarantee. Submitted documents and prompts may be
processed outside the US even though Cloud Run, Cloud SQL, GCS artifacts, and
secrets remain in `us-central1`.

`allow_global_inference` defaults to `false` and accepts only a JSON boolean.
The driver rejects `global` without explicit opt-in, writes the decision into
deployment evidence and Terraform variables, and passes the probe's separate
`MLR_VERTEX_PROBE_ALLOW_GLOBAL=true` flag only for an approved global target.
Terraform independently requires the opt-in before creating or updating Cloud
Run for global inference. The runtime probe verifies the deployed inference
location independently of the job's regional execution location. Model IDs and
CAMS/CommsRx request and response shapes remain unchanged. Keep this candidate
out of `master` and `deploy/production` until the user chooses global processing.

The `mlr-genie/public-ip` GitHub commit status publishes the IP as soon as it is
allocated, before the workflow waits for Cloudflare DNS and Google-managed TLS.
If that status channel is unavailable, Cloud Shell can read the same non-secret
value:

```sh
gcloud compute addresses describe mlr-genie-https --global \
  --project=commsrx-ai --format='value(address)'
```

Deployment waits for the managed certificate and public health/auth checks, runs
the live TypeScript integration and real-journal evaluations, and verifies the
same job, all four artifact hashes, and individual reviewer provenance after a
same-image Cloud Run revision replacement. A short-lived Cloud Run job first
probes Vertex with fixed public synthetic text under the application's runtime
identity. Its bounded, redacted diagnostic helps distinguish provider rejection
from application failures; it does not read documents or authentication secrets.
The temporary job and exact GCS object generation are removed after collection.
The workflow retains only six
explicitly allowlisted sanitized reports; credentials, Terraform state, raw
plans, and raw command logs are excluded. Missing access, DNS, model availability,
or failed checks produce a failed stage rather than a deployment-success claim.

Veeva's optional credential mount defaults off. Runtime memory defaults to 2 GiB
with four concurrent PDF requests per instance. Cloud SQL's production defaults
remain regional HA, two vCPUs/7.5 GiB RAM, backups/PITR, and deletion protection;
these billable capacity settings are visible in the deployment plan.
