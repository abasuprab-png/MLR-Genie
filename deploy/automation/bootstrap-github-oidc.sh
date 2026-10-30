#!/usr/bin/env bash
# Run in an already-authenticated Google Cloud Shell. No keys are created.
set -euo pipefail

readonly PROJECT_ID="commsrx-ai"
readonly PROJECT_NUMBER="333570013714"
readonly REPOSITORY="abasuprab-png/MLR-Genie"
readonly REPOSITORY_ID="1439565611"
readonly OWNER_ID="227769156"
readonly RELEASE_REF="refs/heads/deploy/production"
readonly POOL_ID="mlr-genie-github"
readonly PROVIDER_ID="github"
readonly SA_ID="mlr-genie-deployer"
readonly SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
readonly PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
readonly PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository_id/${REPOSITORY_ID}"
readonly MARKER="MLR Genie deployment bootstrap: ${REPOSITORY}"
readonly MAPPING="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref"
readonly CONDITION="assertion.repository_id == '${REPOSITORY_ID}' && assertion.repository_owner_id == '${OWNER_ID}' && assertion.ref == '${RELEASE_REF}' && assertion.event_name in ['push', 'workflow_dispatch']"
readonly -a ROLES=(
  roles/serviceusage.serviceUsageAdmin
  roles/iam.serviceAccountAdmin
  roles/resourcemanager.projectIamAdmin
  roles/compute.admin
  roles/run.admin
  roles/storage.admin
  roles/secretmanager.admin
  roles/cloudsql.admin
  roles/artifactregistry.admin
)

dry_run=false
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=true ;;
  -h|--help)
    printf 'Usage: bash %s [--dry-run]\n' "$0"
    printf 'Configure keyless GitHub deployment for %s only. Use the existing Cloud Shell login.\n' "$PROJECT_ID"
    exit 0 ;;
  *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac
if (( $# > 1 )); then printf 'Expected at most one argument.\n' >&2; exit 2; fi

run() {
  if "$dry_run"; then
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

fail() { printf '%s\n' "$*" >&2; exit 1; }

# A missing resource is different from a permission or service error. Never
# turn a denied read into a create/update attempt.
describe_or_absent() {
  local result error_file status
  error_file="$(mktemp)"
  if result="$("$@" 2>"$error_file")"; then
    rm -f "$error_file"
    printf '%s' "$result"
    return
  else
    status=$?
  fi
  if ! grep -q 'PERMISSION_DENIED\|SERVICE_DISABLED\|UNAUTHENTICATED' "$error_file" \
      && grep -q 'NOT_FOUND' "$error_file"; then
    rm -f "$error_file"
    printf '{}'
    return
  fi
  cat "$error_file" >&2
  rm -f "$error_file"
  return "$status"
}

check_owned() {
  local kind="$1" data="$2"
  python3 - "$kind" "$MARKER" "$data" <<'PY'
import json
import sys
kind, marker, raw = sys.argv[1:]
data = json.loads(raw)
if not data:
    raise SystemExit(0)
if data.get("description") != marker:
    raise SystemExit(f"Refusing to take over an existing {kind} without this deployment's ownership marker.")
if data.get("disabled") or data.get("state", "ACTIVE") != "ACTIVE":
    raise SystemExit(f"The managed {kind} is disabled or inactive; refusing to re-enable it automatically.")
PY
}

if "$dry_run"; then
  printf 'Review only: the real run first checks the active login and exact project ID/number.\n'
  run gcloud projects describe "$PROJECT_ID" --project="$PROJECT_ID" --format=json
else
  command -v gcloud >/dev/null || fail 'Run this script in Google Cloud Shell with gcloud installed.'
  command -v python3 >/dev/null || fail 'Python 3 is required.'
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
  [[ -n "$account" && "$account" != *$'\n'* ]] || fail 'Select one signed-in Cloud Shell account before running this script.'
  project="$(gcloud projects describe "$PROJECT_ID" --project="$PROJECT_ID" --format=json)"
  python3 - "$PROJECT_ID" "$PROJECT_NUMBER" "$project" <<'PY'
import json
import sys
expected_id, expected_number, raw = sys.argv[1:]
project = json.loads(raw)
if (project.get("projectId") != expected_id
        or str(project.get("projectNumber")) != expected_number
        or project.get("lifecycleState") != "ACTIVE"):
    raise SystemExit("Project ID, number, or lifecycle state does not match the authorized deployment target.")
PY
  printf 'Verified project %s (%s). Using the existing Cloud Shell login.\n' "$PROJECT_ID" "$PROJECT_NUMBER"
fi

run gcloud services enable iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com serviceusage.googleapis.com --project="$PROJECT_ID" --quiet

sa='{}'; pool='{}'; provider='{}'
if ! "$dry_run"; then
  sa="$(describe_or_absent gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" --format=json)"
  pool="$(describe_or_absent gcloud iam workload-identity-pools describe "$POOL_ID" --location=global --project="$PROJECT_ID" --format=json)"
  check_owned 'service account' "$sa"
  check_owned 'workload identity pool' "$pool"
  if [[ "$pool" != '{}' ]]; then
    providers="$(gcloud iam workload-identity-pools providers list \
      --workload-identity-pool="$POOL_ID" --location=global --project="$PROJECT_ID" --format=json)"
    python3 - "$PROVIDER" "$providers" <<'PY'
import json
import sys
expected, raw = sys.argv[1:]
providers = json.loads(raw)
if not isinstance(providers, list) or any(p.get("name") != expected for p in providers):
    raise SystemExit("Unexpected provider exists in the deployment identity pool; review it before continuing.")
PY
    provider="$(describe_or_absent gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
      --workload-identity-pool="$POOL_ID" --location=global --project="$PROJECT_ID" --format=json)"
    check_owned 'workload identity provider' "$provider"
  fi
fi

if [[ "$sa" == '{}' ]]; then
  run gcloud iam service-accounts create "$SA_ID" --project="$PROJECT_ID" \
    --display-name='MLR Genie deployment' --description="$MARKER" --quiet
fi
if [[ "$pool" == '{}' ]]; then
  run gcloud iam workload-identity-pools create "$POOL_ID" --project="$PROJECT_ID" \
    --location=global --display-name='MLR Genie GitHub' --description="$MARKER" --quiet
fi
provider_action=create-oidc
if [[ "$provider" != '{}' ]]; then provider_action=update-oidc; fi
run gcloud iam workload-identity-pools providers "$provider_action" "$PROVIDER_ID" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" \
  --issuer-uri='https://token.actions.githubusercontent.com' \
  --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION" \
  --description="$MARKER" --quiet

if ! "$dry_run"; then
  policy="$(gcloud iam service-accounts get-iam-policy "$SA_EMAIL" --project="$PROJECT_ID" --format=json)"
  python3 - "$PRINCIPAL" "$policy" <<'PY'
import json
import sys
expected, raw = sys.argv[1:]
for binding in json.loads(raw).get("bindings", []):
    members = binding.get("members", [])
    if binding.get("role") == "roles/iam.workloadIdentityUser" and any(m != expected for m in members):
        raise SystemExit("Unexpected workload identity trust already exists on the deployment account; review it before granting deployment roles.")
    if binding.get("role") == "roles/iam.serviceAccountTokenCreator" and members:
        raise SystemExit("Unexpected direct token-creator trust already exists on the deployment account; review it before continuing.")
PY
fi

run gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --member="$PRINCIPAL" --condition=None --quiet --format=none
for role in "${ROLES[@]}"; do
  run gcloud projects add-iam-policy-binding "$PROJECT_ID" --project="$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None --quiet --format=none
done

if "$dry_run"; then
  printf 'Dry run finished. No login, key, token, IAM, API, or cloud resource changes were performed.\n'
else
  printf '\nKeyless deployment bootstrap completed. No service-account keys or access tokens were exported.\n'
fi
printf 'Project: %s\nProject number: %s\nProvider: %s\nDeployment account: %s\nRelease branch: %s\n' \
  "$PROJECT_ID" "$PROJECT_NUMBER" "$PROVIDER" "$SA_EMAIL" "$RELEASE_REF"
