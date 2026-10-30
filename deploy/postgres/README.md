# PostgreSQL operations

MLR Genie owns the `mlr_genie` logical database and schema. CAMS and CommsRx use
the authenticated integration API; they do not share its database credentials or
tables. A physical PostgreSQL instance could host other applications later, but
their logical databases and roles must remain separate.

Terraform creates PostgreSQL 16 with regional high availability by default,
automatic SSD expansion, seven retained daily backups, seven days of point-in-time
recovery logs, and deletion protection. `database_tier`,
`database_availability_type`, `database_disk_size_gb`, and
`database_deletion_protection` are configurable. Review their cost and recovery
requirements before applying; disabling protection or selecting `ZONAL` reduces
the safeguards of the production defaults.

When deletion protection is enabled, the logical database and IAM database role
also use Terraform's `ABANDON` deletion policy. Otherwise a destroy could delete
the logical database before failing to delete the protected instance. Their data
and schema ownership are preserved, although restoring service after an abandoned
deployment still requires recreating the service account's access and importing
the retained resources into Terraform state as appropriate.

The runtime service account has `cloudsql.client` and `cloudsql.instanceUser`.
Its IAM database role omits the `.gserviceaccount.com` email suffix. Cloud Run
mounts its managed Cloud SQL Auth Proxy socket at `/cloudsql`; connector
enforcement blocks direct PostgreSQL clients. The application obtains short-lived
IAM login tokens through Application Default Credentials. No database password
or access token is stored in Terraform variables, configuration, or state.

## Provision and bootstrap before starting the revision

Schema grants are SQL operations, separate from Cloud IAM and the IAM SQL-user
resource. An authorized database administrator must run [bootstrap.sql](bootstrap.sql)
before the application starts. The administrator creates and retains ownership
of the schema and grants the runtime role `USAGE` and `CREATE`. It does not need
membership in, or permission to assume, the runtime role. Runtime startup creates
and owns its tables inside that pre-created schema and must not fall back to
SQLite if PostgreSQL is unavailable.

The permission regression test uses an empty, disposable PostgreSQL 16 database
named `mlr_genie`, separate from the ordinary storage-test database. It reproduces
the old schema-authorization failure with a restricted administrator, then checks
repeated bootstrap and runtime-owned table creation and job persistence:

```sh
MLR_GENIE_TEST_BOOTSTRAP_POSTGRES_URL='<disposable-admin-connection>' \
  python -m pytest tests/test_postgres_bootstrap.py -q
```

Provision the database, its IAM role, runtime role bindings, artifact bucket,
secrets, and required APIs first. On a first deployment, a reviewed staged
Terraform apply can provision this dependency set before applying Cloud Run and
the HTTPS frontend. Targeted applies are a bootstrap exception; inspect the full
plan afterward. Schema bootstrap cannot be represented by a Terraform resource
dependency here, so a single first apply could start an unready revision.

Useful non-secret outputs are:

```sh
terraform -chdir=deploy/terraform output -raw database_connection_name
terraform -chdir=deploy/terraform output -raw database_name
terraform -chdir=deploy/terraform output -raw database_iam_role
terraform -chdir=deploy/terraform output -raw artifacts_bucket_name
```

On an administrator's workstation, run Cloud SQL Auth Proxy v2 with an approved
identity and the returned connection name. Use an existing database administrator
configured through the organization's secure process; this repository does not
create or distribute administrator passwords. For example, in separate terminals:

```sh
cloud-sql-proxy --address 127.0.0.1 --port 55432 '<database_connection_name>'
```

```sh
psql -h 127.0.0.1 -p 55432 -U '<database-admin-role>' -d mlr_genie \
  -v runtime_role='<database_iam_role>' -f deploy/postgres/bootstrap.sql
```

Allow `psql` to prompt for the administrator credential, or use the organization's
approved credential mechanism. Do not put credentials on the command line or in
Terraform. An IAM administrator may use the proxy's `--auto-iam-authn` option and
its own appropriately privileged database role instead.

Before routing production traffic, verify schema grants, runtime IAM login,
table creation, and persistence of job JSON, stored receipts, and private artifact
manifests through independent connections.
Create artifacts on one revision and download them from another, then replace a
revision and repeat. Exercise token expiry and IAM credential refresh, not only
initial login. Cloud SQL model/region access and real backups/restores require
cloud-side verification; local PostgreSQL tests do not establish those results.

## Existing data and recovery

Changing `DATABASE_URL` does not migrate existing SQLite jobs or local artifact
files. If an earlier deployment has real data, preserve a copy, migrate job JSON
and its receipts with tenant boundaries intact, upload their artifact trees,
and verify counts, hashes, references, and authorized downloads before switching
traffic. Do not discard the old source until reconciliation and rollback checks
are complete.

This PostgreSQL store persists the job JSON and private artifact manifest, not a
standalone `idempotency_records` table. External connector `external_store` and
webhook deduplication are existing process-local simulators outside the
CAMS/CommsRx path; their state is not migrated or made durable by this rollout.

Test Cloud SQL point-in-time recovery into a separate instance and verify the
application against the recovered database before a production incident. The
artifact bucket is private, versioned, and has 30-day soft deletion; it is not a
write-once retention-locked archive. Coordinate database recovery with artifact
generations and their recorded hashes. Deletion protection does not replace a
tested backup and restore process.
