"""Set MLR_GENIE_TEST_POSTGRES_URL to a dedicated PostgreSQL test database.

The fixture bootstraps its schema and clears only mlr_genie.jobs. Production
schema/role provisioning is deliberately outside the runtime store.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from backend.domain.schemas import JobRecord, SectorType
from backend.postgres_storage import PostgresJobStore
from backend.storage import (
    CrossTenantAccessError,
    DurableJobStore,
    JobConflictError,
    JobUpdateConflictError,
    StorageUnavailableError,
    create_job_store,
)


def make_job(job_id="shared-job", tenant="tenant-a"):
    return JobRecord(
        job_id=job_id, tenant_id=tenant, title="Original", sector=SectorType.CORPORATE,
        document_type="Brief", destination_platform="package_mode",
        deadline_iso="2026-12-01T00:00:00Z",
    )


@pytest.fixture
def postgres_url():
    url = os.getenv("MLR_GENIE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("MLR_GENIE_TEST_POSTGRES_URL is required for PostgreSQL integration tests")
    with psycopg.connect(url) as connection:
        connection.execute("CREATE SCHEMA IF NOT EXISTS mlr_genie")
    store = PostgresJobStore(url, iam_auth=False)
    store.clear_all_for_test()
    yield url
    store.clear_all_for_test()


def test_storage_factory_never_silently_downgrades_an_unknown_url(tmp_path):
    store = create_job_store(f"sqlite:///{tmp_path / 'prototype.sqlite3'}")
    assert isinstance(store, DurableJobStore)
    for invalid in ("", "mysql://user:secret@host/db", "postgresql+psycopg://user:secret@host/db"):
        with pytest.raises(ValueError) as error:
            create_job_store(invalid)
        assert "secret" not in str(error.value)


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_storage_factory_routes_both_postgres_url_schemes(monkeypatch, scheme):
    constructor = MagicMock()
    monkeypatch.setattr("backend.postgres_storage.PostgresJobStore", constructor)
    url = f"{scheme}://user:secret@host/db"
    assert create_job_store(url) is constructor.return_value
    constructor.assert_called_once_with(url)


def test_private_storage_metadata_is_not_public_job_json():
    job = make_job()
    job._storage_revision = 8
    job._artifact_manifest = {"draft.docx": {"object": "private/object", "generation": 1}}
    cloned = job.model_copy(deep=True)
    assert cloned._storage_revision == 8
    assert cloned._artifact_manifest == job._artifact_manifest
    cloned._artifact_manifest["draft.docx"]["generation"] = 2
    assert job._artifact_manifest["draft.docx"]["generation"] == 1
    for public in (job.model_dump(), JobRecord.model_json_schema().get("properties", {})):
        assert "_storage_revision" not in public and "_artifact_manifest" not in public
    assert "private/object" not in job.model_dump_json()
    forged = JobRecord.model_validate({**job.model_dump(), "_storage_revision": 42,
                                      "_artifact_manifest": {"forged": {}}})
    assert forged._storage_revision is None and forged._artifact_manifest == {}


def test_independent_store_reads_are_detached_and_never_flush(postgres_url):
    first, second = PostgresJobStore(postgres_url), PostgresJobStore(postgres_url)
    first.create_job_strict(make_job())
    with psycopg.connect(postgres_url) as connection:
        before = connection.execute("SELECT revision, updated_at FROM mlr_genie.jobs").fetchone()
    detached = first["shared-job"]
    detached.title = "Unsaved mutation"
    assert first["shared-job"].title == second["shared-job"].title == "Original"
    assert len(first) == 1 and list(first) == ["shared-job"]
    assert first.get_for_tenant("shared-job", "tenant-a").title == "Original"
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT revision, updated_at FROM mlr_genie.jobs").fetchone() == before


def test_stale_update_conflicts_instead_of_losing_newer_work(postgres_url):
    first, second = PostgresJobStore(postgres_url), PostgresJobStore(postgres_url)
    created = first.create_job_strict(make_job())
    assert created._storage_revision == 1
    stale = first[created.job_id]
    changed = second[created.job_id].model_copy(deep=True)
    changed.title = "Newer saved title"
    second.save_job(changed)
    assert changed._storage_revision == 2
    assert first.get_for_tenant(created.job_id, "tenant-a").title == "Newer saved title"
    stale.title = "Older edit"
    with pytest.raises(JobUpdateConflictError):
        first.save_job(stale)
    assert stale._storage_revision == 1
    assert second[created.job_id].title == "Newer saved title"


def test_deleted_job_cannot_be_resurrected_by_read_or_stale_save(postgres_url):
    first, second = PostgresJobStore(postgres_url), PostgresJobStore(postgres_url)
    first.create_job_strict(make_job())
    stale = first["shared-job"]
    del second["shared-job"]
    assert len(first) == 0 and first.get("shared-job") is None
    with pytest.raises(JobUpdateConflictError):
        first.save_job(stale)
    assert second.get("shared-job") is None


def test_concurrent_intake_creates_exactly_one_job(postgres_url):
    stores = [PostgresJobStore(postgres_url), PostgresJobStore(postgres_url)]
    barrier = Barrier(2)

    def create(index):
        barrier.wait(timeout=5)
        try:
            stores[index].create_job_strict(make_job())
            return "created"
        except JobConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == ["conflict", "created"]
    assert len(stores[0]) == 1


def test_tenant_isolation_and_immutable_ownership(postgres_url):
    store = PostgresJobStore(postgres_url)
    with pytest.raises(CrossTenantAccessError):
        store.create_job_strict(make_job(), requesting_tenant_id="tenant-b")
    store.create_job_strict(make_job())
    with pytest.raises(CrossTenantAccessError):
        store.get_for_tenant("shared-job", "tenant-b")
    with pytest.raises(CrossTenantAccessError):
        store.get_for_tenant("shared-job", None)
    changed = store["shared-job"]
    changed.tenant_id = "tenant-b"
    with pytest.raises(CrossTenantAccessError):
        store.save_job(changed)
    assert store["shared-job"].tenant_id == "tenant-a"
    with pytest.raises(JobConflictError):
        store.save_job(make_job())  # A revisionless record cannot overwrite a row.


def test_artifact_manifest_persists_separately_and_hook_runs_on_create_and_update(postgres_url):
    prepared = []

    def prepare(job):
        prepared.append(job.title)
        job._artifact_manifest["draft.docx"] = {"object": f"immutable/{len(prepared)}", "generation": 5}

    first = PostgresJobStore(postgres_url, before_save=prepare)
    first.create_job_strict(make_job())
    second = PostgresJobStore(postgres_url)
    job = second["shared-job"]
    assert job._artifact_manifest["draft.docx"]["object"] == "immutable/1"
    job.title = "Second revision"
    first.save_job(job)
    assert prepared == ["Original", "Second revision"]
    reloaded = second["shared-job"]
    assert reloaded._storage_revision == 2
    assert reloaded._artifact_manifest["draft.docx"]["object"] == "immutable/2"
    with psycopg.connect(postgres_url) as connection:
        payload, manifest = connection.execute(
            "SELECT payload_json, artifact_manifest FROM mlr_genie.jobs"
        ).fetchone()
    assert "_artifact_manifest" not in payload and manifest == reloaded._artifact_manifest


def test_failed_before_save_hook_does_not_commit_job_or_manifest(postgres_url):
    store = PostgresJobStore(postgres_url)
    original = make_job()
    original._artifact_manifest = {"old": {"generation": 1}}
    store.create_job_strict(original)

    def fail(job):
        job._artifact_manifest = {"new": {"generation": 2}}
        raise StorageUnavailableError("Artifact upload unavailable.")

    store.before_save = fail
    changed = store["shared-job"]
    changed.title = "Must not publish"
    with pytest.raises(StorageUnavailableError):
        store.save_job(changed)
    reloaded = store["shared-job"]
    assert reloaded.title == "Original" and reloaded._storage_revision == 1
    assert reloaded._artifact_manifest == {"old": {"generation": 1}}
    with pytest.raises(StorageUnavailableError):
        store.create_job_strict(make_job("failed-create"))
    assert store.get("failed-create") is None


def test_tenant_job_listing_rebuilds_reuse_state_from_shared_storage(postgres_url):
    first = PostgresJobStore(postgres_url)
    for job_id, tenant in (("a-1", "a"), ("a-2", "a"), ("b-1", "b")):
        first.create_job_strict(make_job(job_id, tenant))
    second = PostgresJobStore(postgres_url)
    jobs = second.jobs_for_tenant("a")
    assert {job.job_id for job in jobs} == {"a-1", "a-2"}
    assert all(job._storage_revision == 1 for job in jobs)
    jobs[0].metadata["unsaved"] = True
    assert "unsaved" not in second[jobs[0].job_id].metadata


def test_database_failures_never_expose_dsn_password(monkeypatch):
    secret_url = "postgresql://user:never-print-this@host/db"

    def fail(*args, **kwargs):
        raise psycopg.OperationalError(f"Could not connect to {secret_url}")

    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(StorageUnavailableError) as error:
        PostgresJobStore(secret_url)
    assert "Bootstrap the mlr_genie schema" in str(error.value)
    assert "never-print-this" not in str(error.value)


def test_cloud_sql_iam_uses_refreshable_scoped_credentials(monkeypatch):
    monkeypatch.setattr(PostgresJobStore, "_init_db", lambda self: None)
    credentials = SimpleNamespace(valid=False, token=None)

    def refresh(request):
        credentials.token = "short-lived-token"
        credentials.valid = True

    credentials.refresh = MagicMock(side_effect=refresh)
    default = MagicMock(return_value=(credentials, "project"))
    monkeypatch.setattr("google.auth.default", default)
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresJobStore("postgresql://iam-user@/db?host=/cloudsql/project:region:instance", iam_auth=True)
    with store._connection():
        pass
    with store._connection():
        pass
    default.assert_called_once_with(scopes=["https://www.googleapis.com/auth/sqlservice.login"])
    assert credentials.refresh.call_count == 1
    assert all(call.kwargs["password"] == "short-lived-token" for call in connect.call_args_list)
    credentials.valid = False
    with store._connection():
        pass
    assert credentials.refresh.call_count == 2
