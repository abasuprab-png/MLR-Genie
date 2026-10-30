"""Request-local document workspaces backed by immutable, private GCS objects.

Document generation continues to use ordinary paths. A job's server-owned
manifest is published in the same database update as its review record; objects
uploaded before an unsuccessful database update remain unreachable orphans.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from backend.domain.schemas import JobRecord
from backend.storage import JobConflictError


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ARTIFACT_MEDIA_TYPES = {
    "01_working_draft_v1.docx": DOCX_MEDIA_TYPE,
    "02_reference_source.pdf": "application/pdf",
    "03_annotated_reference.pdf": "application/pdf",
    "04_revised_clean_v2.docx": DOCX_MEDIA_TYPE,
    "05_tracked_changes_redline_v1_to_v2.docx": DOCX_MEDIA_TYPE,
    "06_resubmission_package.zip": "application/zip",
    "07_audit_summary.pdf": "application/pdf",
}


class ArtifactStorageError(RuntimeError):
    """Artifact configuration, publication, or integrity is unavailable."""


class ArtifactConflictError(JobConflictError):
    """A request's staged files no longer match the job it is saving."""


@dataclass
class _StagedArtifacts:
    directory: Path
    tenant_id: str | None
    manifest: dict[str, dict[str, Any]]


@dataclass
class _Workspace:
    entries: dict[tuple[int, str], _StagedArtifacts] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)
    temporary: tempfile.TemporaryDirectory | None = None

    def root(self) -> Path:
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="mlr-artifacts-")
        return Path(self.temporary.name)

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()


_current_workspace: ContextVar[_Workspace | None] = ContextVar("mlr_artifact_workspace", default=None)


@contextmanager
def artifact_workspace() -> Iterator[None]:
    """Isolate one operation's files and retain them until its response finishes."""
    workspace = _Workspace()
    token = _current_workspace.set(workspace)
    try:
        yield
    finally:
        _current_workspace.reset(token)
        workspace.close()


class ArtifactWorkspaceMiddleware:
    """Keep FileResponse paths alive through the complete ASGI response body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        with artifact_workspace():
            await self.app(scope, receive, send)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace() -> _Workspace:
    workspace = _current_workspace.get()
    if workspace is None:
        raise ArtifactStorageError("Shared artifacts require an active request workspace.")
    return workspace


def _bucket_name(value: str) -> str:
    if not isinstance(value, str):
        raise ArtifactStorageError("MLR_GCS_BUCKET must name a GCS bucket without an object prefix.")
    name = value.removeprefix("gs://")
    if name.endswith("/"):
        name = name[:-1]
    if (not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", name)
            or ("." not in name and len(name) > 63)
            or any(len(component) > 63 for component in name.split("."))):
        raise ArtifactStorageError("MLR_GCS_BUCKET must name a GCS bucket without an object prefix.")
    return name


class GcsArtifactStorage:
    """Stage trusted manifests; publish changed files under unique GCS keys."""

    def __init__(self, bucket: str, client=None):
        self.bucket_name = _bucket_name(bucket)
        try:
            if client is None:
                from google.cloud import storage
                client = storage.Client()
            self._bucket = client.bucket(self.bucket_name)
        except Exception:
            raise ArtifactStorageError("Shared artifact storage could not be initialized.") from None

    @staticmethod
    def _prefix(tenant_id: str, job_id: str) -> str:
        tenant = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        job = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return f"tenants/{tenant}/jobs/{job}/artifacts/"

    def _manifest(self, job: JobRecord, manifest: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(manifest, dict):
            raise ArtifactStorageError("Stored artifact manifest is invalid.")
        validated = {}
        prefix = self._prefix(job.tenant_id, job.job_id)
        for name, item in manifest.items():
            if name not in ARTIFACT_MEDIA_TYPES or not isinstance(item, dict):
                raise ArtifactStorageError("Stored artifact manifest is invalid.")
            object_name = item.get("object")
            generation = item.get("generation")
            if type(generation) is int:
                generation = str(generation)
            size = item.get("bytes")
            sha256 = item.get("sha256")
            if (not isinstance(object_name, str)
                    or not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}/" + re.escape(name), object_name)
                    or not isinstance(generation, str) or not re.fullmatch(r"[1-9][0-9]*", generation)
                    or type(size) is not int or size < 0
                    or not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256)):
                raise ArtifactStorageError("Stored artifact manifest is invalid.")
            validated[name] = {"object": object_name, "generation": generation,
                               "sha256": sha256, "bytes": size}
        return validated

    def directory(self, job_id: str, store, *, job: JobRecord | None = None) -> Path:
        """Fetch a published set, optionally pinned to an authorized job snapshot."""
        workspace = _workspace()
        key = (id(self), job_id)
        with workspace.lock:
            if job is not None and job.job_id != job_id:
                raise ArtifactConflictError("Artifact snapshot belongs to a different job.")
            if key in workspace.entries:
                entry = workspace.entries[key]
                if job is not None and (entry.tenant_id != job.tenant_id
                        or self._manifest(job, job._artifact_manifest) != entry.manifest):
                    raise ArtifactConflictError("Published artifacts changed during this operation; reload the job and retry.")
                return entry.directory
            if job is None:
                job = store.get(job_id)
            manifest = self._manifest(job, job._artifact_manifest) if job is not None else {}
            tenant_id = job.tenant_id if job is not None else None
            directory = workspace.root() / f"{id(self):x}-{hashlib.sha256(job_id.encode('utf-8')).hexdigest()}"
            directory.mkdir()
            try:
                for name, descriptor in manifest.items():
                    path = directory / name
                    generation = int(descriptor["generation"])
                    blob = self._bucket.blob(descriptor["object"], generation=generation)
                    blob.download_to_filename(str(path), if_generation_match=generation, checksum="auto")
                    if path.stat().st_size != descriptor["bytes"] or _digest_file(path) != descriptor["sha256"]:
                        raise ArtifactStorageError("Stored artifact integrity verification failed.")
            except ArtifactStorageError:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise ArtifactStorageError("Published artifacts could not be retrieved.") from None
            workspace.entries[key] = _StagedArtifacts(directory, tenant_id, manifest)
            return directory

    def prepare_save(self, job: JobRecord) -> None:
        """Upload a complete candidate set before the store's conditional commit.

        Uploads never replace existing objects. A failure preserves both the job's
        previous manifest and the workspace baseline; successful but uncommitted
        uploads are harmless orphans until a database save publishes their keys.
        """
        current_manifest = self._manifest(job, job._artifact_manifest)
        workspace = _current_workspace.get()
        if workspace is None:
            return
        with workspace.lock:
            entry = workspace.entries.get((id(self), job.job_id))
            if entry is None:
                return
            if entry.tenant_id is not None and entry.tenant_id != job.tenant_id:
                raise ArtifactConflictError("Artifact workspace belongs to a different tenant.")
            if current_manifest != entry.manifest:
                raise ArtifactConflictError("Published artifacts changed during this operation; reload the job and retry.")
            pending = {}
            try:
                for name, media_type in ARTIFACT_MEDIA_TYPES.items():
                    path = entry.directory / name
                    if path.is_symlink():
                        raise ArtifactStorageError("Artifact workspaces must contain regular files.")
                    if not path.exists():
                        continue
                    if not path.is_file():
                        raise ArtifactStorageError("Artifact workspaces must contain regular files.")
                    size = path.stat().st_size
                    sha256 = _digest_file(path)
                    previous = entry.manifest.get(name)
                    if previous and previous["bytes"] == size and previous["sha256"] == sha256:
                        pending[name] = dict(previous)
                        continue
                    object_name = self._prefix(job.tenant_id, job.job_id) + uuid4().hex + "/" + name
                    blob = self._bucket.blob(object_name)
                    blob.upload_from_filename(str(path), content_type=media_type,
                                              if_generation_match=0, checksum="auto")
                    if blob.generation is None:
                        blob.reload()
                    generation = str(blob.generation)
                    if not re.fullmatch(r"[1-9][0-9]*", generation):
                        raise ArtifactStorageError("Artifact publication returned an invalid generation.")
                    pending[name] = {"object": object_name, "generation": generation,
                                     "sha256": sha256, "bytes": size}
            except ArtifactStorageError:
                raise
            except Exception:
                raise ArtifactStorageError("Artifacts could not be published.") from None
            job._artifact_manifest = deepcopy(pending)
            entry.manifest = deepcopy(pending)
            entry.tenant_id = job.tenant_id
