"""Shared job persistence with detached reads and optimistic concurrency control."""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.domain.schemas import JobRecord
from backend.storage import (
    CrossTenantAccessError,
    JobConflictError,
    JobUpdateConflictError,
    StorageUnavailableError,
)


class PostgresJobStore(MutableMapping[str, JobRecord]):
    """One authoritative row per job, shared safely by independent API instances.

    The database/schema and grants are provisioned separately. The runtime role
    needs USAGE and CREATE on the dedicated ``mlr_genie`` schema, plus access to
    its jobs table. Reads never mutate storage or retain mutable job instances.
    """

    def __init__(
        self,
        database_url: str,
        *,
        before_save: Callable[[JobRecord], None] | None = None,
        iam_auth: bool | None = None,
    ) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgreSQL storage requires a postgresql:// or postgres:// URL.")
        self._database_url = database_url
        self.before_save = before_save
        self._iam_auth = (
            os.getenv("MLR_GENIE_DB_IAM_AUTH", "false").lower() in ("1", "true", "yes")
            if iam_auth is None else iam_auth
        )
        self._credentials: Any = None
        self._credentials_lock = threading.Lock()
        try:
            self._init_db()
        except StorageUnavailableError:
            raise StorageUnavailableError(
                "PostgreSQL job storage initialization failed. Bootstrap the mlr_genie schema "
                "and grant the runtime role USAGE and CREATE before starting the API."
            ) from None

    def _iam_password(self) -> str:
        import google.auth
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request

        try:
            with self._credentials_lock:
                if self._credentials is None:
                    self._credentials, _ = google.auth.default(
                        scopes=["https://www.googleapis.com/auth/sqlservice.login"]
                    )
                if not self._credentials.valid or not self._credentials.token:
                    self._credentials.refresh(Request())
                return self._credentials.token
        except GoogleAuthError:
            raise StorageUnavailableError("PostgreSQL IAM authentication is unavailable.") from None

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        options: dict[str, Any] = {"row_factory": dict_row, "connect_timeout": 10}
        if self._iam_auth:
            # Cloud Run's Cloud SQL Unix socket provides the authenticated, TLS
            # protected proxy transport; the short-lived token is the DB password.
            options["password"] = self._iam_password()
        try:
            with psycopg.connect(self._database_url, **options) as connection:
                yield connection
        except psycopg.Error:
            raise StorageUnavailableError("PostgreSQL job storage is unavailable.") from None

    def _init_db(self) -> None:
        with self._connection() as connection:
            # Cold starts may initialize together. PostgreSQL's IF NOT EXISTS
            # alone does not serialize concurrent creation of the same table.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('mlr_genie.jobs.bootstrap'))")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mlr_genie.jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    revision BIGINT NOT NULL CHECK (revision > 0),
                    artifact_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _job_from_row(row: dict[str, Any]) -> JobRecord:
        job = JobRecord.model_validate(row["payload_json"])
        job._storage_revision = row["revision"]
        job._artifact_manifest = deepcopy(row["artifact_manifest"])
        return job

    def _prepare_save(self, job: JobRecord) -> None:
        if self.before_save is not None:
            self.before_save(job)

    def create_job_strict(self, job: JobRecord, requesting_tenant_id: str | None = None) -> JobRecord:
        if requesting_tenant_id is not None and requesting_tenant_id != job.tenant_id:
            raise CrossTenantAccessError("Cannot create a job for another tenant.")
        self._prepare_save(job)
        with self._connection() as connection:
            inserted = connection.execute(
                """
                INSERT INTO mlr_genie.jobs (job_id, tenant_id, payload_json, revision, artifact_manifest)
                VALUES (%s, %s, %s, 1, %s)
                ON CONFLICT (job_id) DO NOTHING
                RETURNING revision
                """,
                (job.job_id, job.tenant_id, Jsonb(job.model_dump(mode="json")), Jsonb(job._artifact_manifest)),
            ).fetchone()
            if inserted is None:
                raise JobConflictError("Job ID already exists; intake cannot overwrite an existing job.")
        job._storage_revision = inserted["revision"]
        return job

    def save_job(self, job: JobRecord) -> JobRecord:
        if job._storage_revision is None:
            return self.create_job_strict(job)
        expected_revision = job._storage_revision
        self._prepare_save(job)
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE mlr_genie.jobs
                SET payload_json = %s, artifact_manifest = %s, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND tenant_id = %s AND revision = %s
                RETURNING revision
                """,
                (Jsonb(job.model_dump(mode="json")), Jsonb(job._artifact_manifest),
                 job.job_id, job.tenant_id, expected_revision),
            ).fetchone()
            if updated is None:
                owner = connection.execute(
                    "SELECT tenant_id FROM mlr_genie.jobs WHERE job_id = %s", (job.job_id,)
                ).fetchone()
                if owner is not None and owner["tenant_id"] != job.tenant_id:
                    raise CrossTenantAccessError("A job's tenant cannot be changed.")
                raise JobUpdateConflictError("Job changed or was deleted; reload it before retrying.")
        job._storage_revision = updated["revision"]
        return job

    def get_for_tenant(self, job_id: str, requesting_tenant_id: str | None) -> JobRecord:
        job = self[job_id]
        if requesting_tenant_id is None or job.tenant_id != requesting_tenant_id:
            raise CrossTenantAccessError("Cross-tenant job access denied.")
        return job

    def jobs_for_tenant(self, tenant_id: str) -> list[JobRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json, revision, artifact_manifest FROM mlr_genie.jobs "
                "WHERE tenant_id = %s ORDER BY updated_at DESC, job_id",
                (tenant_id,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def __getitem__(self, key: str) -> JobRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json, revision, artifact_manifest FROM mlr_genie.jobs WHERE job_id = %s",
                (key,),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return self._job_from_row(row)

    def __setitem__(self, key: str, value: JobRecord) -> None:
        if key != value.job_id:
            raise ValueError("The mapping key must match the job ID.")
        self.save_job(value)

    def __delitem__(self, key: str) -> None:
        with self._connection() as connection:
            deleted = connection.execute(
                "DELETE FROM mlr_genie.jobs WHERE job_id = %s RETURNING job_id", (key,)
            ).fetchone()
        if deleted is None:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM mlr_genie.jobs ORDER BY updated_at DESC, job_id"
            ).fetchall()
        return iter(row["job_id"] for row in rows)

    def __len__(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM mlr_genie.jobs").fetchone()
        return row["count"]

    def clear_all_for_test(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM mlr_genie.jobs")
