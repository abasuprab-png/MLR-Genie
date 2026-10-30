"""Hosted configuration and server-owned human-acceptance provenance."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.domain.schemas import JobRecord, ReviewComment


LOCAL_SIGNING_SECRET = "mlr-genie-hmac-sha256-default-signing-key-v1"
ACCEPTANCE_AUTHORIZATIONS = "accepted_comment_authorizations"
REVIEWER_ROLES = frozenset(("reviewer", "approver"))


class SecurityConfigurationError(RuntimeError):
    pass


def hosted_mode() -> bool:
    mode = os.getenv("MLR_GENIE_ENV", "local").strip().lower() or "local"
    if mode not in ("local", "hosted"):
        raise SecurityConfigurationError("MLR_GENIE_ENV must be local or hosted.")
    return mode == "hosted"


def signing_secret() -> bytes:
    value = os.getenv("MLR_GENIE_SESSION_SECRET", "")
    if hosted_mode() and (
        not value.strip() or len(value.encode("utf-8")) < 32 or value.strip() == LOCAL_SIGNING_SECRET
    ):
        raise SecurityConfigurationError("Hosted mode requires a private MLR_GENIE_SESSION_SECRET of at least 32 bytes.")
    return (value or LOCAL_SIGNING_SECRET).encode("utf-8")


def signed_bearer_required() -> bool:
    return hosted_mode() or os.getenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", "").strip().lower() in ("1", "true", "yes")


def cors_policy() -> tuple[list[str], bool]:
    hosted = hosted_mode()
    configured = os.getenv("MLR_GENIE_ALLOWED_ORIGINS", "")
    if not hosted and not configured.strip():
        return ["*"], True
    origins = []
    for entry in configured.split(","):
        origin = entry.strip().rstrip("/")
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.path
                     and not parsed.query and not parsed.fragment and "*" not in origin)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise SecurityConfigurationError("MLR_GENIE_ALLOWED_ORIGINS must list explicit HTTP(S) origins, without wildcards.")
        origins.append(origin)
    # Server integrations and Authorization headers do not need cookie credentials.
    return origins, False


def validate_security_startup() -> None:
    secret = signing_secret()
    origins, _ = cors_policy()
    if hosted_mode():
        return
    relaxed = []
    if secret == LOCAL_SIGNING_SECRET.encode() or not secret.strip() or len(secret) < 32:
        relaxed.append("MLR_GENIE_SESSION_SECRET (public or weak local signing key)")
    if not os.getenv("MLR_GENIE_BOOTSTRAP_SECRET", "").strip():
        relaxed.append("MLR_GENIE_BOOTSTRAP_SECRET (open local issuer)")
    if not signed_bearer_required():
        relaxed.append("MLR_REQUIRE_SIGNED_BEARER_TOKEN (identity headers/query authentication allowed)")
    if "*" in origins:
        relaxed.append("MLR_GENIE_ALLOWED_ORIGINS (wildcard local CORS)")
    if relaxed:
        logging.getLogger(__name__).warning("Local development security settings: %s", "; ".join(relaxed))


class ConfiguredCORSMiddleware:
    """Apply the explicit origin policy outside authentication, including preflights."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            origins, credentials = cors_policy()
        except SecurityConfigurationError as exc:
            return await JSONResponse(status_code=503, content={"detail": str(exc)})(scope, receive, send)
        cors = CORSMiddleware(self.app, allow_origins=origins, allow_credentials=credentials,
                              allow_methods=["*"], allow_headers=["*"])
        await cors(scope, receive, send)


def comment_digest(comment: ReviewComment) -> str:
    payload = {key: comment.model_dump()[key] for key in (
        "comment_id", "reviewer_name", "reviewer_role", "timestamp", "anchored_passage",
        "target_sentence_id", "comment_text", "proposed_spans",
    )}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def record_acceptance(job: JobRecord, comment: ReviewComment, *, actor: str, role: str) -> None:
    job.metadata.setdefault(ACCEPTANCE_AUTHORIZATIONS, {})[comment.comment_id] = {
        "actor": actor, "role": role, "comment_sha256": comment_digest(comment),
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


def has_authorized_acceptance(job: JobRecord, comment: ReviewComment) -> bool:
    receipt = job.metadata.get(ACCEPTANCE_AUTHORIZATIONS, {}).get(comment.comment_id, {})
    return bool(receipt.get("role") in REVIEWER_ROLES and comment.human_actor
                and receipt.get("actor") == comment.human_actor
                and receipt.get("comment_sha256") == comment_digest(comment))


def artifact_directory(root: Path, job_id: str) -> Path:
    """Keep portable legacy IDs; give every other ID its own non-colliding namespace."""
    if len(job_id) <= 180 and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", job_id):
        return root / job_id
    return root / ".opaque_job_ids" / hashlib.sha256(job_id.encode("utf-8")).hexdigest()
