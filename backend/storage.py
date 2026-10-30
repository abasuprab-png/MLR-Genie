from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

from backend.domain.schemas import JobRecord


class JobConflictError(Exception):
    """Raised when an intake attempts to overwrite an existing job ID."""


class CrossTenantAccessError(PermissionError):
    """Raised when a principal from one tenant attempts to access another tenant's job."""


class DurableJobStore(MutableMapping[str, JobRecord]):
    """
    SQLite-backed durable job store implementing MutableMapping[str, JobRecord].
    - Persists every JobRecord transactionally on disk so state survives module
      reinitialization and process restarts (Audit Probe 27).
    - Keeps an in-memory cache synchronized with SQLite so in-place mutations on
      fetched JobRecord instances are flushed and persisted automatically.
    - Enforces tenant isolation (Audit Probe 7) and job_id collision protection (Audit Probe 8).
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_url = os.getenv("DATABASE_URL", "sqlite:///./mlr_genie_authoritative.db")
            if db_url.startswith("sqlite:///"):
                resolved = db_url.replace("sqlite:///", "", 1)
            else:
                resolved = "./mlr_genie_authoritative.db"
            self.db_path = Path(resolved).resolve()
        else:
            self.db_path = Path(db_path).resolve()

        self._cache: dict[str, JobRecord] = {}
        self._init_db()
        self._load_all_from_disk()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    scoped_key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    platform_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    external_object_id TEXT NOT NULL,
                    external_version TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load_all_from_disk(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT job_id, payload_json FROM jobs").fetchall()
            for row in rows:
                try:
                    data = json.loads(row["payload_json"])
                    self._cache[row["job_id"]] = JobRecord.model_validate(data)
                except Exception:
                    pass

    def flush(self, job_id: str | None = None) -> None:
        """Persists cached JobRecord(s) to SQLite."""
        targets = [job_id] if job_id and job_id in self._cache else list(self._cache.keys())
        if not targets:
            return
        with self._connect() as conn:
            for jid in targets:
                job = self._cache[jid]
                conn.execute(
                    """
                    INSERT INTO jobs (job_id, tenant_id, payload_json, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(job_id) DO UPDATE SET
                        tenant_id = excluded.tenant_id,
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (job.job_id, job.tenant_id, job.model_dump_json()),
                )
            conn.commit()

    def save_job(self, job: JobRecord) -> JobRecord:
        """Persists an updated JobRecord in the in-memory cache and SQLite database."""
        self._cache[job.job_id] = job
        self.flush(job.job_id)
        return job

    def create_job_strict(self, job: JobRecord, requesting_tenant_id: str | None = None) -> JobRecord:
        """
        Inserts a new job only if `job_id` does not already exist (Audit Probe 8: Job-ID collision).
        Also enforces that `requesting_tenant_id` (if provided) matches `job.tenant_id`.
        """
        if requesting_tenant_id and requesting_tenant_id != job.tenant_id:
            raise CrossTenantAccessError(
                f"Tenant '{requesting_tenant_id}' cannot create a job owned by tenant '{job.tenant_id}'."
            )
        self.flush()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT tenant_id FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
            if existing is not None:
                raise JobConflictError(
                    f"Job ID '{job.job_id}' already exists (owned by tenant '{existing['tenant_id']}'). "
                    "Refusing to overwrite existing job."
                )
            conn.execute(
                """
                INSERT INTO jobs (job_id, tenant_id, payload_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (job.job_id, job.tenant_id, job.model_dump_json()),
            )
            conn.commit()
        self._cache[job.job_id] = job
        return job

    def get_for_tenant(self, job_id: str, requesting_tenant_id: str | None) -> JobRecord:
        """
        Retrieves a job and verifies tenant ownership (Audit Probe 7).
        """
        self.flush()
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"Job '{job_id}' not found.")
        if not requesting_tenant_id or requesting_tenant_id != job.tenant_id:
            raise CrossTenantAccessError(
                f"Cross-tenant access denied: principal tenant '{requesting_tenant_id}' "
                f"cannot access job '{job_id}' owned by '{job.tenant_id}'."
            )
        return job

    def __getitem__(self, key: str) -> JobRecord:
        if key in self._cache:
            self.flush(key)
            return self._cache[key]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE job_id = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            job = JobRecord.model_validate(json.loads(row["payload_json"]))
            self._cache[key] = job
            return job

    def __setitem__(self, key: str, value: JobRecord) -> None:
        self._cache[key] = value
        self.flush(key)

    def __delitem__(self, key: str) -> None:
        self._cache.pop(key, None)
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (key,))
            conn.commit()

    def __iter__(self) -> Iterator[str]:
        self.flush()
        with self._connect() as conn:
            rows = conn.execute("SELECT job_id FROM jobs ORDER BY updated_at DESC").fetchall()
            keys = [r["job_id"] for r in rows]
        for k in keys:
            if k not in self._cache:
                try:
                    _ = self[k]
                except KeyError:
                    continue
            yield k

    def __len__(self) -> int:
        self.flush()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM jobs").fetchone()
            return int(row["cnt"]) if row else 0

    def clear_all_for_test(self) -> None:
        self._cache.clear()
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM idempotency_records")
            conn.commit()
