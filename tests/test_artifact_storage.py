from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.artifact_storage import (
    ARTIFACT_MEDIA_TYPES,
    ArtifactConflictError,
    ArtifactStorageError,
    ArtifactWorkspaceMiddleware,
    GcsArtifactStorage,
    artifact_workspace,
)


class FakeGcsClient:
    """Generation-aware SDK double, also used by the shared persistence tests."""

    def __init__(self):
        self.objects: dict[tuple[str, str, int], bytes] = {}
        self.content_types = {}
        self.upload_count = 0
        self.download_count = 0
        self.fail_upload_after: int | None = None
        self.fail_download = False
        self.download_preconditions = []
        self._generation = 0

    def bucket(self, name):
        return FakeGcsBucket(self, name)


class FakeGcsBucket:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def blob(self, name, generation=None):
        return FakeGcsBlob(self, name, generation)


class FakeGcsBlob:
    def __init__(self, bucket, name, generation=None):
        self.bucket = bucket
        self.name = name
        self.generation = generation

    def upload_from_filename(self, filename, *, content_type, if_generation_match, checksum):
        client = self.bucket.client
        assert if_generation_match == 0 and checksum == "auto"
        if client.fail_upload_after is not None and client.upload_count >= client.fail_upload_after:
            raise RuntimeError("credential-detail-that-must-not-be-returned")
        assert not any(bucket == self.bucket.name and name == self.name for bucket, name, _ in client.objects)
        client._generation += 1
        self.generation = client._generation
        key = (self.bucket.name, self.name, self.generation)
        client.objects[key] = Path(filename).read_bytes()
        client.content_types[key] = content_type
        client.upload_count += 1

    def download_to_filename(self, filename, *, if_generation_match, checksum):
        client = self.bucket.client
        assert if_generation_match == self.generation and checksum == "auto"
        if client.fail_download:
            raise RuntimeError("credential-detail-that-must-not-be-returned")
        client.download_preconditions.append(if_generation_match)
        Path(filename).write_bytes(client.objects[(self.bucket.name, self.name, self.generation)])
        client.download_count += 1


@dataclass
class Record:
    job_id: str = "review/a:b"
    tenant_id: str = "tenant-a"
    _artifact_manifest: dict = field(default_factory=dict)


class MemoryStore:
    def __init__(self, *jobs):
        self.jobs = {job.job_id: deepcopy(job) for job in jobs}

    def get(self, job_id):
        return deepcopy(self.jobs.get(job_id))

    def commit(self, job):
        self.jobs[job.job_id] = deepcopy(job)


@pytest.fixture
def shared():
    client = FakeGcsClient()
    storage = GcsArtifactStorage("gs://mlr-test-artifacts", client=client)
    job = Record()
    store = MemoryStore(job)
    return client, storage, job, store


def publish_fixture(storage, job, store, files):
    with artifact_workspace():
        directory = storage.directory(job.job_id, store)
        for name, content in files.items():
            (directory / name).write_bytes(content)
        storage.prepare_save(job)
        store.commit(job)
    return deepcopy(job._artifact_manifest)


def test_new_instance_recovers_all_seven_files_and_preserves_unchanged_generations(shared):
    client, storage, job, store = shared
    content = {name: name.encode() for name in ARTIFACT_MEDIA_TYPES}
    original = publish_fixture(storage, job, store, content)
    assert client.upload_count == 7
    fresh = GcsArtifactStorage("mlr-test-artifacts", client=client)
    with artifact_workspace():
        directory = fresh.directory(job.job_id, store)
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == content
        loaded = store.get(job.job_id)
        fresh.prepare_save(loaded)
        assert client.upload_count == 7
        assert loaded._artifact_manifest == original
        (directory / "07_audit_summary.pdf").write_bytes(b"new reviewer audit")
        (directory / "06_resubmission_package.zip").unlink()
        fresh.prepare_save(loaded)
        store.commit(loaded)
        revised = loaded._artifact_manifest
        assert "06_resubmission_package.zip" not in revised
        assert revised["07_audit_summary.pdf"]["object"] != original["07_audit_summary.pdf"]["object"]
        assert revised["01_working_draft_v1.docx"] == original["01_working_draft_v1.docx"]
    assert not directory.exists()
    assert client.upload_count == 8
    # Prior versions remain intact; a failed DB publication can still use them.
    for name, descriptor in original.items():
        assert client.objects[(storage.bucket_name, descriptor["object"], int(descriptor["generation"]))] == content[name]
    assert set(client.download_preconditions) == {int(d["generation"]) for d in original.values()}


def test_partial_upload_failure_does_not_publish_or_mutate_previous_manifest(shared):
    client, storage, job, store = shared
    files = {"01_working_draft_v1.docx": b"old draft", "07_audit_summary.pdf": b"old audit"}
    original = publish_fixture(storage, job, store, files)
    with artifact_workspace():
        directory = storage.directory(job.job_id, store)
        for name in files:
            (directory / name).write_bytes(b"changed " + name.encode())
        loaded = store.get(job.job_id)
        client.fail_upload_after = client.upload_count + 1
        with pytest.raises(ArtifactStorageError, match="^Artifacts could not be published.$") as error:
            storage.prepare_save(loaded)
        assert "credential-detail" not in str(error.value)
        assert loaded._artifact_manifest == original
        assert store.get(job.job_id)._artifact_manifest == original
        # The first changed upload is an orphan, not an overwrite of old bytes.
        assert len(client.objects) == 3
    client.fail_upload_after = None
    with artifact_workspace():
        directory = storage.directory(job.job_id, store)
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == files


def test_successful_upload_followed_by_failed_database_commit_keeps_old_set_usable(shared):
    client, storage, job, store = shared
    original = publish_fixture(storage, job, store, {"02_reference_source.pdf": b"published source"})
    with artifact_workspace():
        directory = storage.directory(job.job_id, store)
        (directory / "02_reference_source.pdf").write_bytes(b"uncommitted source")
        loaded = store.get(job.job_id)
        storage.prepare_save(loaded)
        assert loaded._artifact_manifest != original
        # Simulate a database CAS failure by leaving the previous record committed.
        assert store.get(job.job_id)._artifact_manifest == original
    with artifact_workspace():
        assert (storage.directory(job.job_id, store) / "02_reference_source.pdf").read_bytes() == b"published source"
    assert len(client.objects) == 2


def test_nested_reload_after_competing_publication_cannot_replace_newer_artifacts(shared):
    client, storage, job, store = shared
    publish_fixture(storage, job, store, {"01_working_draft_v1.docx": b"original"})
    with artifact_workspace():
        outer = storage.directory(job.job_id, store)
        (outer / "01_working_draft_v1.docx").write_bytes(b"stale outer result")
        with artifact_workspace():
            other = storage.directory(job.job_id, store)
            (other / "01_working_draft_v1.docx").write_bytes(b"new committed draft")
            competing = store.get(job.job_id)
            storage.prepare_save(competing)
            store.commit(competing)
        uploads = client.upload_count
        reloaded = store.get(job.job_id)
        with pytest.raises(ArtifactConflictError, match="changed during"):
            storage.prepare_save(reloaded)
        assert client.upload_count == uploads
    with artifact_workspace():
        assert (storage.directory(job.job_id, store) / "01_working_draft_v1.docx").read_bytes() == b"new committed draft"


def test_pinned_snapshot_keeps_record_and_files_consistent_after_peer_publication(shared):
    _, storage, job, store = shared
    publish_fixture(storage, job, store, {"02_reference_source.pdf": b"original source"})
    authorized_snapshot = store.get(job.job_id)
    peer = store.get(job.job_id)
    publish_fixture(storage, peer, store, {"02_reference_source.pdf": b"new source"})
    assert authorized_snapshot._artifact_manifest != store.get(job.job_id)._artifact_manifest

    def unexpected_read(job_id):
        raise AssertionError("Pinned snapshot must not re-read the mutable database record.")

    store.get = unexpected_read
    with artifact_workspace():
        directory = storage.directory(job.job_id, store, job=authorized_snapshot)
        assert (directory / "02_reference_source.pdf").read_bytes() == b"original source"


def test_explicit_newer_snapshot_cannot_reuse_an_older_staged_workspace(shared):
    _, storage, job, store = shared
    publish_fixture(storage, job, store, {"02_reference_source.pdf": b"original source"})
    snapshot = store.get(job.job_id)
    with artifact_workspace():
        directory = storage.directory(job.job_id, store, job=snapshot)
        peer = store.get(job.job_id)
        publish_fixture(storage, peer, store, {"02_reference_source.pdf": b"new source"})
        with pytest.raises(ArtifactConflictError, match="changed during"):
            storage.directory(job.job_id, store, job=store.get(job.job_id))
        assert (directory / "02_reference_source.pdf").read_bytes() == b"original source"


@pytest.mark.parametrize("change", ["cross_job", "cross_tenant", "filename", "generation", "hash", "size"])
def test_rejects_forged_manifest_ownership_names_and_descriptors(shared, change):
    client, storage, job, store = shared
    publish_fixture(storage, job, store, {"02_reference_source.pdf": b"source"})
    record = store.get(job.job_id)
    descriptor = record._artifact_manifest["02_reference_source.pdf"]
    if change == "cross_job":
        record.job_id = "another-job"
    elif change == "cross_tenant":
        record.tenant_id = "another-tenant"
    elif change == "filename":
        record._artifact_manifest["../source.pdf"] = record._artifact_manifest.pop("02_reference_source.pdf")
    elif change == "generation":
        descriptor["generation"] = True
    elif change == "hash":
        descriptor["sha256"] = "invalid"
    else:
        descriptor["bytes"] = -1
    store.commit(record)
    before = client.download_count
    with artifact_workspace(), pytest.raises(ArtifactStorageError, match="manifest is invalid"):
        storage.directory(record.job_id, store)
    assert client.download_count == before


@pytest.mark.parametrize("mutation", [b"xxxxxx", b"short"])
def test_downloads_verify_recorded_hash_and_size(shared, mutation):
    client, storage, job, store = shared
    manifest = publish_fixture(storage, job, store, {"02_reference_source.pdf": b"source"})
    descriptor = manifest["02_reference_source.pdf"]
    client.objects[(storage.bucket_name, descriptor["object"], int(descriptor["generation"]))] = mutation
    with artifact_workspace(), pytest.raises(ArtifactStorageError, match="integrity verification failed"):
        storage.directory(job.job_id, store)


def test_download_errors_are_sanitized_and_partial_workspace_can_be_retried(shared):
    client, storage, job, store = shared
    publish_fixture(storage, job, store, {"02_reference_source.pdf": b"source"})
    with artifact_workspace():
        client.fail_download = True
        with pytest.raises(ArtifactStorageError, match="^Published artifacts could not be retrieved.$"):
            storage.directory(job.job_id, store)
        client.fail_download = False
        assert (storage.directory(job.job_id, store) / "02_reference_source.pdf").read_bytes() == b"source"


def test_workspaces_are_independent_and_context_propagates_to_sync_worker(shared):
    _, storage, job, store = shared
    with artifact_workspace():
        outer = storage.directory(job.job_id, store)
        (outer / "01_working_draft_v1.docx").write_bytes(b"outer")
        with artifact_workspace():
            inner = storage.directory(job.job_id, store)
            assert inner != outer and not list(inner.iterdir())
        assert not inner.exists() and outer.exists()

        async def worker():
            return await asyncio.to_thread(storage.directory, job.job_id, store)

        assert asyncio.run(worker()) == outer
        assert (outer / "01_working_draft_v1.docx").read_bytes() == b"outer"
    assert not outer.exists()
    with pytest.raises(ArtifactStorageError, match="active request workspace"):
        storage.directory(job.job_id, store)


@pytest.mark.parametrize("raise_after_send", [False, True])
def test_asgi_workspace_lives_until_response_finishes_and_always_cleans_up(shared, raise_after_send):
    _, storage, job, store = shared
    paths = []

    async def app(scope, receive, send):
        path = storage.directory(job.job_id, store) / "07_audit_summary.pdf"
        path.write_bytes(b"response bytes")
        paths.append(path)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": path.read_bytes(), "more_body": False})
        assert path.exists()
        if raise_after_send:
            raise RuntimeError("response failed")

    async def send(message):
        assert paths[0].exists()

    async def receive():
        return {"type": "http.request", "body": b""}

    call = ArtifactWorkspaceMiddleware(app)({"type": "http"}, receive, send)
    if raise_after_send:
        with pytest.raises(RuntimeError, match="response failed"):
            asyncio.run(call)
    else:
        asyncio.run(call)
    assert not paths[0].exists()


def test_symbolic_links_cannot_publish_outside_workspace(shared, tmp_path):
    client, storage, job, store = shared
    outside = tmp_path / "private-source"
    outside.write_bytes(b"private data")
    with artifact_workspace():
        directory = storage.directory(job.job_id, store)
        (directory / "02_reference_source.pdf").symlink_to(outside)
        with pytest.raises(ArtifactStorageError, match="regular files"):
            storage.prepare_save(job)
    assert client.upload_count == 0 and job._artifact_manifest == {}


@pytest.mark.parametrize("bucket", ["", "gs://bucket/path", "https://bucket", "BUCKET", "aa", "bucket?token=secret"])
def test_bucket_configuration_is_a_name_only(bucket):
    with pytest.raises(ArtifactStorageError, match="without an object prefix"):
        GcsArtifactStorage(bucket, client=FakeGcsClient())
