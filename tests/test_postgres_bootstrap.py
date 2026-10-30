"""Opt-in permission regression against an empty, disposable PostgreSQL database.

Set MLR_GENIE_TEST_BOOTSTRAP_POSTGRES_URL to an administrator connection for an
empty database named mlr_genie. This test creates roles and a schema and removes
them afterward. The ordinary storage-test database must not be used here.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote, urlsplit

import psycopg
from psycopg import sql
import pytest

from backend.domain.schemas import JobRecord, SectorType
from backend.postgres_storage import PostgresJobStore


def test_bootstrap_and_runtime_work_without_admin_runtime_role_membership():
    url = os.getenv("MLR_GENIE_TEST_BOOTSTRAP_POSTGRES_URL")
    if not url:
        pytest.skip("Requires an empty disposable PostgreSQL bootstrap test database")
    spec = importlib.util.spec_from_file_location(
        "mlr_bootstrap_test_driver", Path(__file__).resolve().parents[1] / "deploy/automation/deploy_gcp.py"
    )
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    suffix = secrets.token_hex(6)
    admin, runtime = "schema_admin_" + suffix, "runtime_" + suffix
    password = secrets.token_urlsafe(32)
    parsed_url = urlsplit(url)
    host = parsed_url.netloc.rsplit("@", 1)[-1]
    def role_url(role):
        return parsed_url._replace(netloc=f"{quote(role, safe='')}:{quote(password, safe='')}@{host}").geturl()
    admin_url, runtime_url = role_url(admin), role_url(runtime)
    with psycopg.connect(url, autocommit=True) as root:
        assert root.execute("SELECT current_database()").fetchone()[0] == "mlr_genie"
        assert root.execute("SELECT to_regnamespace('mlr_genie')").fetchone()[0] is None, (
            "Use an empty disposable database; existing schemas are never overwritten."
        )
        try:
            for role in (admin, runtime):
                root.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB PASSWORD {}")
                             .format(sql.Identifier(role), sql.Literal(password)))
            root.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE mlr_genie TO {} WITH GRANT OPTION")
                         .format(sql.Identifier(admin)))
            with psycopg.connect(admin_url, autocommit=True) as connection:
                assert not connection.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (runtime,)).fetchone()[0]
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(sql.SQL("CREATE SCHEMA mlr_genie AUTHORIZATION {}")
                                       .format(sql.Identifier(runtime)))
                connection.execute(driver.bootstrap_sql(runtime))
                connection.execute(driver.bootstrap_sql(runtime))  # repeated deployment
                assert connection.execute(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='mlr_genie'"
                ).fetchone()[0] == admin
            store = PostgresJobStore(runtime_url, iam_auth=False)
            job = JobRecord(
                job_id="bootstrap-permission-check", tenant_id="synthetic-bootstrap",
                title="Before", sector=SectorType.CORPORATE, document_type="Brief",
                destination_platform="package_mode", deadline_iso="2026-12-01T00:00:00Z",
            )
            store.create_job_strict(job)
            loaded = PostgresJobStore(runtime_url, iam_auth=False)[job.job_id]
            loaded.title = "After"
            store.save_job(loaded)
            assert PostgresJobStore(runtime_url, iam_auth=False)[job.job_id].title == "After"
            with psycopg.connect(runtime_url) as connection:
                assert connection.execute(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='mlr_genie.jobs'::regclass"
                ).fetchone()[0] == runtime
            del store[job.job_id]
            assert store.get(job.job_id) is None
        finally:
            root.execute("DROP SCHEMA IF EXISTS mlr_genie CASCADE")
            # Remove grants issued by the restricted administrator as well as
            # the database owner's grants before dropping these temporary roles.
            for role in (admin, runtime):
                root.execute(sql.SQL("REVOKE ALL ON DATABASE mlr_genie FROM {} CASCADE")
                             .format(sql.Identifier(role)))
            for role in (runtime, admin):
                root.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                root.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
