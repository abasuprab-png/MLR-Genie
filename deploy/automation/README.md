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
