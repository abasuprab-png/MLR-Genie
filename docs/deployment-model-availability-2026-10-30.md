# Production deployment: model availability decision

Verified October 30, 2026 (America/New_York).

The MLR Genie integration API is provisioned in `commsrx-ai`
(`333570013714`) at `https://mlr-genie.signalfidelitygroup.com`.
The deployment is not fully verified: the configured deep-reasoning model is
unavailable through the US inference endpoint.

## Verified behavior

- Public HTTPS `/health` returns 200 and reports the intended project.
- Tokenless CAMS requests return 401; direct public `run.app` access returns 404.
- Cloud Run retains internal-and-load-balancer ingress and application bearer
  authentication. The organization's domain-restricted IAM policy remains intact.
- Cloud SQL PostgreSQL, its schema grants, runtime IAM login, and private GCS
  storage are provisioned. The runtime diagnostic can write its private result.
- Python, TypeScript, client build, and Terraform preflight checks pass.
- The live fast-gate model works through the US endpoint under the runtime
  service account.

The full CAMS/CommsRx roundtrip, real-journal verification, and cross-revision
job/artifact persistence checks have not passed yet.

## Confirmed model restriction

The configured logical deep-reasoning role `gemini-4-argon` maps to
`gemini-3.1-pro-preview`. A synthetic request under the application's runtime
identity returned HTTP 404 for:

`projects/commsrx-ai/locations/us/publishers/google/models/gemini-3.1-pro-preview`

The diagnostic contained no customer documents or bootstrap credentials. Its
temporary Cloud Run job and exact GCS output generation were removed afterward.
The diagnostic is retained with
[deployment run 41136753055](https://github.com/abasuprab-png/MLR-Genie/actions/runs/41136753055).

Google's [model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-pro)
lists this preview model for `global`. Google's
[location guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations)
states that global endpoints do not provide control over the region used for ML
processing. US multi-region endpoints provide a US processing boundary.

## Prepared option

The isolated candidate keeps the current model and selects global Gemini
processing with an explicit `allow_global_inference` flag. The flag defaults to
false, and both deployment validation and the synthetic probe reject a global
configuration unless that explicit flag is set.

Cloud Run, PostgreSQL, and the private artifact bucket remain in `us-central1`.
Gemini requests and provider-side context caching would use the global location;
customer document text could therefore be processed outside the US.
Global model access for this project still requires live verification; this
candidate has not made any global inference calls.

The alternative is to retain US-only inference and select and validate a
US-supported deep-reasoning model. The current model has not been silently
replaced, and global processing has not been deployed. Either selection still
requires the remaining live acceptance checks before declaring deployment complete.
