# Production-readiness implementation and verification — October 30, 2026

Subsequent container and durable-storage implementation is recorded in the
[deployment follow-up](durable-deployment-verification-2026-10-30.md).

The four changes requested after `b51cba2` are implemented. Production readiness
remains **blocked on deployment verification**: no public hostname or GCP identity
was configured in this environment, and production deployment was explicitly a
separate authorized step. No Terraform apply, image deployment, or production
secret provisioning was performed.

## Changes and security boundary

The previous unset environment selected prototype authentication. The default is
now hosted; Cloud Run refuses explicit local mode. Implicit local operation needs
`MLR_GENIE_ALLOW_LOCAL=true` and the actual loopback socket registered by
`python -m backend.server`. Unknown and public bind addresses retain hosted rules.
Explicit `MLR_GENIE_ENV=local` retains deliberate development behavior outside
Cloud Run. Startup logs the resolved mode; the launcher exits unsuccessfully when
startup security validation fails.

Both the HTTP token issuer and shared signing helper default to `requester` and
validate the allowed roles (`requester`, `service`, `reviewer`, `approver`). Invalid
roles return HTTP 422. Hosted token issuance has a 3600-second maximum by default;
`MLR_GENIE_MAX_TOKEN_TTL_SECONDS` accepts positive integer limits up to 86400.
Excessive lifetimes return 422, including an omitted 3600-second default when a
smaller cap is configured. Local lifetimes remain positive and uncapped. Reviewer
authority still requires an explicit individual reviewer/approver token.

The preferred global external HTTPS load balancer uses a serverless NEG and a
Google-managed certificate for the required `public_host`. Cloud Run retains
internal-and-load-balancer ingress. Its invoker grant permits the load balancer
to forward application bearer tokens. Cloud Armor throttles `/auth/*` at 60
requests/minute/IP and `/integrations/*` at 600. See
[deployment instructions](../deploy/terraform/README.md).

The shared security and signing boundaries avoid integration-specific workarounds.
The CAMS and CommsRx input/output models and standalone TypeScript client are
unchanged. Legacy development tests and the isolated live red-team evaluator now
opt into local behavior explicitly.

Changed files cover `backend/main.py`, `backend/security.py`, new
`backend/server.py`; the HTTPS Terraform resources, provider lock, ignores, and
deployment README; both live evaluators; seven existing test modules; the root
README, `.env.example`, hardening guide, and integration README. No generated demo
artifacts are included.

## Ordered verification

1. Syntax, types, and configuration: Python `compileall`, `git diff --check`,
   `npm run check`, and `npm run client:check` passed. Terraform 1.14.0 with signed
   Google provider 6.50.0 passed `terraform init -backend=false`,
   `terraform fmt -check`, and `terraform validate`; the provider lock is retained.
2. Security regression checks: omitted-role acceptance returns 403; unknown/blank
   roles and excessive/noninteger lifetimes return 422; requests at the default,
   configured, and 86400-second caps succeed. Tests include a smaller configured
   cap, direct signer calls, alternate acceptance input forms, blank environment
   settings, and Cloud Run markers. Real processes verified loopback startup and
   mode logging, public-bind refusal without hosted secrets, and Cloud Run local
   refusal; both refused processes exited 3.
3. Legitimate behavior and package checks:
   `GEMINI_API_KEY= GOOGLE_GENAI_USE_VERTEXAI=false .venv/bin/pytest -v`
   passed **240 tests**. The launcher tests were rerun after its final failure-exit
   adjustment (**2 passed**). `npm test` passed **32 tests** and
   `npm run client:build` passed. Unit tests deliberately disable paid model calls;
   live verification is separate. Existing Starlette/Vite deprecation warnings
   remain. Network-enabled execution permissions were needed for test-server
   sockets and Terraform provider handshakes.

The final hosted smoke command was
`.venv/bin/python evals/live_integrations.py --hosted`. It used a real loopback HTTP
server, temporary database and random in-memory signing/bootstrap credentials,
the compiled TypeScript client, and unmocked Gemini. It proved health 200,
tokenless CAMS 401, omitted-role `requester` acceptance 403, explicit requester
CAMS success, and individual reviewer CommsRx success. All four artifacts were
downloaded and their hashes, actual tracked changes, PDF annotations, audit
content, and reviewer provenance checked. The report includes two live claim
receipts and the complete live router telemetry; all recorded backend source
hashes match this implementation. See the
[sanitized hosted evidence](production-readiness-hosted-verification-2026-10-30.json).

Thus the original accidental-local, implicit-reviewer, and unbounded-issued-token
paths no longer reproduce. Deliberate local development, requester verification,
and explicit reviewer/approver acceptance remain functional. A fresh independent
read-only review found no surviving concrete bypass or regression after the
legacy evaluator startup issue was corrected and its loopback setup checked.

## Deployment checks still pending

Supply the controlled hostname, GCP deployment identity, immutable image digest,
and existing runtime prerequisites described in the deployment README. Existing
configuration also references the Veeva secret and Vertex AI: verify the runtime
service account has their required access before deployment. This ingress patch
does not expand those unrelated existing application permissions.

After a separately authorized deployment, wait for DNS and an ACTIVE certificate;
from outside Google Cloud check health 200 and tokenless CAMS 401. With the deployed
bootstrap secret supplied securely, run:

```sh
.venv/bin/python evals/live_integrations.py --base-url https://<public-host>
```

This external option never starts a local service or invents remote credentials.
The smoke fixture is synthetic. Additionally record the actual public host,
deployed image digest, and one successful requester `verify-source` call using a
real journal PDF and a reworded claim. Those results, Azure reachability, and live
Cloud Armor throttling are not established by local tests or Terraform validation.

Rotate `MLR_GENIE_SESSION_SECRET` during rollout to invalidate previously issued
tokens: issuance limits do not retroactively shorten their lifetimes. Rotation
revokes every outstanding session token, so consumers must obtain new tokens.
