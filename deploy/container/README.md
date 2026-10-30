# Production image packaging

Build from the repository root:

```sh
docker build -t mlr-genie:readiness .
```

In the managed Codex environment, supply its combined public CA trust bundle
through BuildKit without copying it into the image:

```sh
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t mlr-genie:readiness .
```

The image installs only the Python runtime package and its dependencies. The
build context excludes environment files, credentials, local databases, uploaded
documents, test artifacts, frontend dependencies, and repository metadata. The
runtime uses UID/GID 10001, binds `0.0.0.0`, and listens on `PORT` (8080 by default).
Hosted mode is the image default. Inject private signing and bootstrap secrets
at runtime using the platform's secret manager; never pass them as build args.
Startup refuses a missing or unsafe signing secret.

The source package explicitly includes `backend` and `evals`: the backend imports
two evaluation modules during startup. A wheel build should be checked from a
working directory outside the checkout so local source cannot hide missing code.

For local smoke tests, the default SQLite database at
`/data/mlr_genie_authoritative.db` and artifacts at `/app/artifacts/file_roundtrip`
are instance-local data. Production Terraform configures the supported PostgreSQL
and GCS backends and requires them with `MLR_GENIE_REQUIRE_DURABLE_STORAGE=true`.
That setting refuses startup with local storage. PostgreSQL uses a service-owned
database and IAM login; GCS stores immutable artifacts referenced by a private
manifest in the matching job update. See [database setup](../postgres/README.md)
and [deployment configuration](../terraform/README.md). Cloud Run's filesystem
holds request-scoped staging files, which are removed after response delivery.

Live Vertex operation separately requires its project API/access configuration,
runtime permissions, `GOOGLE_GENAI_LIVE_VERTEX=true`, and
`MLR_REQUIRE_LIVE_GEMINI=true`. The container smoke check can leave the provider
unconfigured to verify startup, health, and authentication without model calls.

For an authorized deployment, push the built image to the selected registry,
resolve the registry manifest digest, and provide `repository@sha256:digest` as
Terraform's `app_image`. A local Docker image ID is not that registry digest.
