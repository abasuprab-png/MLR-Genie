# US inference acceptance

Updated October 30, 2026 (America/New_York).

The integration API is deployed at
`https://mlr-genie.signalfidelitygroup.com` in project `commsrx-ai`
(`333570013714`). The user's latest selection requires US-only AI processing.

Production run [41148099165](https://github.com/abasuprab-png/MLR-Genie/actions/runs/41148099165)
passed HTTPS/authentication, CAMS/CommsRx, real-journal PDF grounding, and
cross-revision PostgreSQL/GCS persistence. That run used global inference and
therefore does not establish acceptance of the US-only requirement.

## Correction

The old SDK model map silently redirected `gemini-4-argon` to
`gemini-3.1-pro-preview`. The preview returned HTTP 404 through the US endpoint.
That result establishes a restriction on the preview, rather than on the actual
Argon model. The obsolete redirect is removed, so the effective model now matches
the configured Argon role and its telemetry.

Google's current [Argon model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-argon)
lists `gemini-4-argon` as GA with US multi-region processing and GenerateContent
support. The production target selects `us` and explicitly disables global
inference. Cloud Run, Cloud SQL, and private artifact storage remain in
`us-central1`.

US/EU requests use stateless GenerateContent. The preview
[Interactions API](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/interactions)
does not commit to residency for session storage. The
[location guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations)
defines `aiplatform.us.rep.googleapis.com` as the US processing endpoint.
Retries use the same client and model; a provider rejection cannot trigger a
global fallback.

## Validation

Installed-SDK transport tests verify US/EU hostnames, actual Argon model IDs,
structured schemas, safety settings, expired-cache retry routing, and rejection
without a global retry. The production workflow must still verify model access
under the runtime service account, the external integration and journal flows,
and persistence across revision replacement for this release. Its sanitized
artifact is the authoritative live acceptance record.
