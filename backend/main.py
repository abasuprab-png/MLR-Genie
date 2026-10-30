from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.agent.mlr_genie_agent import MlrGenieAgent
from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.document_engine import (
    build_resubmission_package_zip,
    create_born_digital_reference_pdf,
    create_working_draft_docx,
    extract_pdf_source_pack,
    inspect_pdf_document_and_extract_source_pack,
    parse_docx_working_document,
    render_annotated_reference_pdf,
    sha256_file,
    write_clean_revised_docx,
    write_tracked_changes_redline_docx,
)
from backend.domain.schemas import (
    ConnectorActionType,
    JobLifecycleState,
    JobRecord,
    ProvenanceEventType,
    ReviewComment,
    StatementReuseRecord,
)
from backend.storage import CrossTenantAccessError, DurableJobStore, JobConflictError
from backend.tools.document_tools import (
    apply_accepted_changes,
    import_rd_handoff_pack,
    ingest_working_document,
    locate_and_anchor_evidence,
    reanchor_after_reflow,
    split_medical_sentences,
)
from backend.tools.platform_adapters import CapabilityCheckedPlatformAdapter
from backend.tools.review_tools import (
    import_and_reconcile_comments,
    screen_pharmacovigilance_cv08,
    validate_strategy,
)
from evals.acceptance_suite import run_all_12_acceptance_cases
from evals.pharma_comms_scenario_battery import run_pharma_comms_scenario_battery

app = FastAPI(
    title="MLR Genie — Google Cloud, Gemini & ADK Backend",
    version="3.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = GcpAgentConfig()
router = GeminiModelRouter(config)
genie = MlrGenieAgent(config=config, router=router)

genie.register_tool("ingest_working_document", ingest_working_document)
genie.register_tool("locate_and_anchor_evidence", locate_and_anchor_evidence)
genie.register_tool("apply_accepted_changes", apply_accepted_changes)
genie.register_tool("reanchor_after_reflow", reanchor_after_reflow)
genie.register_tool("import_rd_handoff_pack", import_rd_handoff_pack)
genie.register_tool("screen_pharmacovigilance_cv08", screen_pharmacovigilance_cv08)
genie.register_tool("validate_strategy", validate_strategy)
genie.register_tool("import_and_reconcile_comments", import_and_reconcile_comments)

# Durable SQLite-backed store (survives module reload & process restart — Probe 27)
JOBS_DB = DurableJobStore()
TENANT_REUSE_LIBRARIES: dict[str, list[StatementReuseRecord]] = {}

_APRIMO_DAM_ADAPTER = CapabilityCheckedPlatformAdapter("aprimo_dam")

ADAPTERS = {
    "veeva_promomats": CapabilityCheckedPlatformAdapter("veeva_promomats"),
    "aprimo_dam": _APRIMO_DAM_ADAPTER,
    "aprimo_dam_only": _APRIMO_DAM_ADAPTER,
    "aprimo_productivity": CapabilityCheckedPlatformAdapter("aprimo_productivity"),
    "package_mode": CapabilityCheckedPlatformAdapter("package_mode"),
}

ALLOWED_ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    "01_working_draft_v1.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "02_reference_source.pdf": "application/pdf",
    "03_annotated_reference.pdf": "application/pdf",
    "04_revised_clean_v2.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "05_tracked_changes_redline_v1_to_v2.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "06_resubmission_package.zip": "application/zip",
}


class IntakeRequest(BaseModel):
    job: JobRecord
    initial_body: str
    actor: str
    source_pack: list[dict[str, Any]] = []


class SubmitPlatformBody(BaseModel):
    actor_human: str | None = None
    idempotency_key: str | None = None


class ImportRdHandoffBody(BaseModel):
    source_pack: list[dict[str, Any]] | None = None
    rd_source_pack: list[dict[str, Any]] | None = None
    disease_name: str | None = None
    trial_design_summary: str | None = None
    actor: str | None = None
    human_confirmed: bool = True
    append_to_working_body: bool = True


def _job_artifact_dir(job_id: str) -> Path:
    safe_job_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", job_id)
    return Path(__file__).resolve().parent.parent / "artifacts" / "file_roundtrip" / safe_job_id


def _collect_job_artifacts_metadata(job_id: str) -> dict[str, dict[str, Any]]:
    out_dir = _job_artifact_dir(job_id)
    artifacts: dict[str, dict[str, Any]] = {}
    for fname, media_type in ALLOWED_ARTIFACT_MEDIA_TYPES.items():
        fpath = out_dir / fname
        exists = fpath.exists()
        artifacts[fname] = {
            "filename": fname,
            "exists": exists,
            "bytes": fpath.stat().st_size if exists else 0,
            "sha256": sha256_file(fpath) if exists else None,
            "media_type": media_type,
            "download_url": f"/jobs/{job_id}/artifacts/{fname}",
        }
    return artifacts


import base64
import hashlib
import hmac
import json
import os
import time

def _get_session_token_secret() -> bytes:
    return (
        os.environ.get("MLR_GENIE_SESSION_SECRET")
        or "mlr-genie-hmac-sha256-default-signing-key-v1"
    ).encode("utf-8")


def issue_signed_session_token(
    tenant_id: str,
    actor_id: str,
    role: str = "reviewer",
    ttl_seconds: int = 3600,
) -> str:
    """
    Issues an HMAC-SHA256 signed session token (`mlr1.<base64url_payload>.<base64url_sig>`)
    binding a verified tenant_id, actor_id, role, and expiration timestamp.
    """
    clean_tenant = (tenant_id or "").strip()
    clean_actor = (actor_id or "").strip()
    if not clean_tenant or not clean_actor:
        raise ValueError("Both tenant_id and actor_id are required to issue a signed session token.")
    now = int(time.time())
    payload = {
        "tenant_id": clean_tenant,
        "actor_id": clean_actor,
        "role": (role or "reviewer").strip(),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
    sig = hmac.new(_get_session_token_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"mlr1.{payload_b64}.{sig_b64}"


def verify_signed_session_token(token: str) -> dict[str, Any]:
    """
    Verifies an HMAC-SHA256 signed session token and returns its claims dict.
    Raises HTTPException(status_code=401) if malformed, tampered, or expired.
    """
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != "mlr1" or not parts[1] or not parts[2]:
        raise HTTPException(
            status_code=401,
            detail="Invalid session token format; expected 'Bearer mlr1.<payload>.<signature>'.",
        )
    _, payload_b64, sig_b64 = parts
    expected_sig = hmac.new(
        _get_session_token_secret(),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected_sig_b64, sig_b64):
        raise HTTPException(
            status_code=401,
            detail="Invalid session token signature.",
        )
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Malformed session token payload.",
        ) from exc

    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Invalid session token claims.")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or int(exp) < int(time.time()):
        raise HTTPException(status_code=401, detail="Session token has expired.")

    tenant_id = str(claims.get("tenant_id") or "").strip()
    actor_id = str(claims.get("actor_id") or "").strip()
    if not tenant_id or not actor_id:
        raise HTTPException(status_code=401, detail="Session token missing tenant_id or actor_id.")

    return claims


def _require_authenticated_tenant(
    request: Request,
    expected_tenant_id: str | None = None,
    *,
    explicit_actor: str | None = None,
    allow_query_auth: bool = False,
) -> tuple[str, str]:
    """
    Enforces HTTP authentication and tenant ownership across all job routes:
    1. Never falls back to `expected_tenant_id` or a default actor when credentials are absent.
    2. If `Authorization` header is present, it MUST be a valid `Bearer <signed_session_token>`
       verified via HMAC-SHA256 (`verify_signed_session_token`); arbitrary values raise 401.
    3. Otherwise requires BOTH an explicit tenant header (`X-Tenant-Id` / `X-MLR-Tenant-Id`)
       AND an explicit actor header (`X-Actor-Id` / `X-MLR-Actor`) to be non-empty.
    """
    auth_header = (request.headers.get("authorization") or "").strip()
    header_tenant = (
        request.headers.get("x-tenant-id")
        or request.headers.get("x-mlr-tenant-id")
        or (request.query_params.get("tenant_id") if allow_query_auth else "")
        or ""
    ).strip()
    header_actor = (
        request.headers.get("x-actor-id")
        or request.headers.get("x-mlr-actor")
        or (request.query_params.get("actor_id") if allow_query_auth else "")
        or ""
    ).strip()

    if auth_header:
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header: expected 'Bearer <signed_session_token>'.",
            )
        raw_token = auth_header[7:].strip()
        claims = verify_signed_session_token(raw_token)
        resolved_tenant = str(claims["tenant_id"]).strip()
        resolved_actor = str(claims["actor_id"]).strip()
        if header_tenant and header_tenant != resolved_tenant:
            raise HTTPException(
                status_code=403,
                detail="Header X-Tenant-Id conflicts with signed session token tenant.",
            )
        if header_actor and header_actor != resolved_actor:
            raise HTTPException(
                status_code=403,
                detail="Header X-Actor-Id conflicts with signed session token actor.",
            )
        if explicit_actor and explicit_actor.strip() and explicit_actor.strip() != resolved_actor:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Actor identity mismatch: payload actor '{explicit_actor.strip()}' "
                    f"conflicts with authenticated session actor '{resolved_actor}'."
                ),
            )
        if expected_tenant_id and resolved_tenant != expected_tenant_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Cross-tenant access denied: caller tenant '{resolved_tenant}' "
                    f"cannot access tenant '{expected_tenant_id}'."
                ),
            )
        return resolved_tenant, resolved_actor

    if not header_tenant or not header_actor:
        raise HTTPException(
            status_code=401,
            detail=(
                "Authentication required: provide a valid Bearer session token or both "
                "X-Tenant-Id and X-Actor-Id headers."
            ),
        )

    if explicit_actor and explicit_actor.strip() and explicit_actor.strip() != header_actor:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Actor identity mismatch: payload actor '{explicit_actor.strip()}' "
                f"conflicts with authenticated header actor '{header_actor}'."
            ),
        )

    if expected_tenant_id and header_tenant != expected_tenant_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Cross-tenant access denied: caller tenant '{header_tenant}' "
                f"cannot access tenant '{expected_tenant_id}'."
            ),
        )

    return header_tenant, header_actor


@app.get("/health")
def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "project_id": config.project_id,
        "location": config.location,
        "zero_data_retention": config.zero_data_retention,
        "durable_jobs_count": len(JOBS_DB),
        "models": {
            "deep_reasoning": config.model_deep_reasoning,
            "orchestrator": config.model_orchestrator,
            "fast_gate": config.model_fast_gate,
            "embedding": config.model_embedding,
            "transcribe": config.model_transcribe,
            "managed_sandbox": config.managed_sandbox_agent,
        },
    }


@app.post("/jobs/intake")
def create_job_intake(req: IntakeRequest, request: Request) -> dict[str, Any]:
    job = req.job
    if not req.actor or not req.actor.strip():
        raise HTTPException(status_code=401, detail="Authenticated actor identity is required.")

    caller_tenant, resolved_actor = _require_authenticated_tenant(
        request,
        expected_tenant_id=job.tenant_id,
        explicit_actor=req.actor,
    )

    # Probe 8: Prevent duplicate or cross-tenant job_id overwrite
    existing = JOBS_DB.get(job.job_id)
    if existing is not None:
        if existing.tenant_id != caller_tenant:
            raise HTTPException(
                status_code=403,
                detail=f"Job ID '{job.job_id}' is owned by another tenant.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job.job_id}' already exists; use revision endpoints instead of overwriting intake.",
        )

    pv_result = genie.invoke_tool(
        "screen_pharmacovigilance_cv08",
        job=job,
        intake_text=req.initial_body,
        router=router,
    )

    # Probe 9: Regulatory Stop (CV-08) halts intake immediately before creating working revisions or claims
    if pv_result.get("cv08_flagged"):
        job.state = JobLifecycleState.HOLD
        JOBS_DB.create_job_strict(job)
        return {
            "job": job.model_dump(),
            "pv_screening": pv_result,
        }

    try:
        if req.source_pack:
            job.source_pack = [dict(item) for item in req.source_pack]
        genie.invoke_tool(
            "ingest_working_document",
            job=job,
            body_text=req.initial_body,
            actor=resolved_actor,
            event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
        )
        genie.invoke_tool(
            "locate_and_anchor_evidence",
            job=job,
            required_mandate_action="prepare_annotations",
            source_pack=req.source_pack,
            router=router,
        )
        genie.invoke_tool(
            "validate_strategy",
            job=job,
            text_to_check=req.initial_body,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        JOBS_DB.create_job_strict(job)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "job": job.model_dump(),
        "pv_screening": pv_result,
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, _ = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    try:
        verified_job = JOBS_DB.get_for_tenant(job_id, caller_tenant)
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not verified_job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job": verified_job.model_dump(),
        "artifacts": _collect_job_artifacts_metadata(job_id),
    }


@app.post("/jobs/{job_id}/comments")
def reconcile_job_comments(
    job_id: str,
    comments: list[ReviewComment],
    request: Request,
) -> dict[str, Any]:
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, actor = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        res = genie.invoke_tool(
            "import_and_reconcile_comments",
            job=job,
            raw_comments=comments,
            router=router,
        )
        if job.revisions and any(
            c.human_decision == "accepted" and c.placement_status != "placed"
            for c in job.comments
        ):
            apply_res = apply_accepted_changes(
                job=job,
                comments=job.comments,
                human_actor=actor,
            )
            res["applied_changes"] = {
                "placed_comment_Ids": apply_res["placed_comment_Ids"],
                "unplaced_comment_Ids": apply_res["unplaced_comment_Ids"],
            }
            if apply_res.get("new_revision") is not None:
                out_dir = _job_artifact_dir(job_id)
                ref_pdf = out_dir / "02_reference_source.pdf"
                annotated_pdf = out_dir / "03_annotated_reference.pdf"
                if ref_pdf.exists():
                    job.metadata["last_annotated_pdf"] = render_annotated_reference_pdf(
                        ref_pdf, annotated_pdf, claims=job.claims
                    )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    JOBS_DB.save_job(job)
    return {"reconciliation": res, "job": job.model_dump()}


@app.post("/jobs/{job_id}/import-rd-handoff")
def import_rd_handoff_endpoint(
    job_id: str,
    request: Request,
    body: ImportRdHandoffBody | None = None,
) -> dict[str, Any]:
    """
    Phase 2 -> Phase 3 R&D-to-Comms Handoff Importer endpoint:
    1. Extracts and locks verified epidemiology & disease statistics, trial design parameters,
       and required qualifiers into `job.source_pack`.
    2. Generates a pre-anchored 'About the Disease & Epidemiology Boilerplate' section
       (`rd_handoff_boilerplate`) and seeds verified claims with `SourceAnchor` coordinates.
    3. Seeds verified `StatementReuseRecord` entries (`provenance=ProvenanceEventType.HUMAN_CONFIRMED`)
       into the tenant's Statement Reuse Library.
    """
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    explicit_actor = (body.actor if body and body.actor else None)
    caller_tenant, resolved_actor = _require_authenticated_tenant(
        request,
        expected_tenant_id=job.tenant_id,
        explicit_actor=explicit_actor,
    )
    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if job.pv_icrs_flagged or job.state == JobLifecycleState.HOLD:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job.job_id}' is on Pharmacovigilance (CV-08) hold and cannot import R&D handoff copy.",
        )

    incoming_pack = (
        (body.rd_source_pack if body and body.rd_source_pack is not None else body.source_pack)
        if body
        else None
    )
    disease_name = body.disease_name if body is not None else None
    trial_design_summary = body.trial_design_summary if body is not None else None
    human_confirmed = body.human_confirmed if body is not None else True
    append_body = body.append_to_working_body if body is not None else True
    tenant_lib = TENANT_REUSE_LIBRARIES.setdefault(caller_tenant, [])

    try:
        handoff_res = genie.invoke_tool(
            "import_rd_handoff_pack",
            job=job,
            required_mandate_action="retrieve_sources",
            rd_source_pack=incoming_pack,
            disease_name=disease_name,
            trial_design_summary=trial_design_summary,
            actor=resolved_actor,
            human_confirmed=human_confirmed,
            append_to_working_body=append_body,
            reuse_library=tenant_lib,
            router=router,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    JOBS_DB.save_job(job)
    return {
        "job_id": job.job_id,
        "tenant_id": caller_tenant,
        "rd_handoff": {
            "imported_sources_count": len(handoff_res["locked_source_pack"]),
            "rd_handoff_boilerplate": handoff_res["rd_handoff_boilerplate"],
            "epidemiology_and_trial_summary": handoff_res["epidemiology_and_trial_summary"],
        },
        "rd_handoff_boilerplate": handoff_res["rd_handoff_boilerplate"],
        "locked_source_pack": handoff_res["locked_source_pack"],
        "seeded_claims": handoff_res["seeded_claims"],
        "seeded_reuse_records": handoff_res["seeded_reuse_records"],
        "reuse_library_seeded": handoff_res["seeded_reuse_records"],
        "epidemiology_and_trial_summary": handoff_res["epidemiology_and_trial_summary"],
        "job": job.model_dump(),
    }


@app.post("/jobs/{job_id}/submit/{platform_key}")
def submit_to_platform(
    job_id: str,
    platform_key: str,
    request: Request,
    actor_human: str | None = None,
    idempotency_key: str | None = None,
    body: SubmitPlatformBody | None = None,
) -> dict[str, Any]:
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    resolved_actor_param = (
        actor_human
        or (body.actor_human if body else None)
        or request.headers.get("x-actor-id")
        or request.headers.get("x-mlr-actor")
        or ""
    ).strip()
    caller_tenant, resolved_actor = _require_authenticated_tenant(
        request,
        expected_tenant_id=job.tenant_id,
        explicit_actor=resolved_actor_param,
    )
    resolved_idem = (
        idempotency_key
        or (body.idempotency_key if body else None)
        or f"idem_{job_id}_{platform_key}_v{job.current_version}"
    ).strip()

    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Probe 10: Block submission of PV-flagged / held jobs
    if job.pv_icrs_flagged or job.state == JobLifecycleState.HOLD:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job.job_id}' is on Pharmacovigilance (CV-08) hold and cannot be submitted.",
        )

    adapter = ADAPTERS.get(platform_key)
    if not adapter:
        raise HTTPException(status_code=400, detail=f"Unsupported adapter: {platform_key}")

    try:
        # Enforce ADK before_tool_callback mandate check and adapter execution (Probe 13)
        genie.before_tool_callback(
            tool_name=f"submit_{platform_key}",
            job=job,
            required_mandate_action="upload_draft",
            kwargs={"requesting_tenant_id": caller_tenant, "actor_human": resolved_actor},
        )
        receipt = adapter.execute_action(
            job=job,
            action=ConnectorActionType.UPLOAD_VERSION,
            actor_human=resolved_actor,
            payload={"version": job.current_version, "title": job.title},
            idempotency_key=resolved_idem,
        )
        genie.after_tool_callback(
            tool_name=f"submit_{platform_key}",
            job=job,
            required_mandate_action="upload_draft",
            kwargs={"requesting_tenant_id": caller_tenant, "actor_human": resolved_actor},
            status=receipt.status,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    JOBS_DB.save_job(job)
    return {
        "receipt": receipt.model_dump(),
        "job_state": job.state.value,
        "job": job.model_dump(),
    }


@app.post("/jobs/{job_id}/upload-working-docx")
@app.post("/jobs/{job_id}/files/working-docx")
def upload_working_docx_endpoint(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """
    Accepts a real `.docx` working draft upload from the browser, saves it as
    `01_working_draft_v1.docx`, parses paragraphs/tables/SHA-256 via `python-docx`,
    runs CV-08 PV screening and strategy advisories, and records a human revision.
    """
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, actor = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    raw_bytes = file.file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded .docx file is empty.")

    try:
        import io
        import docx

        docx.Document(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid .docx file: {exc}") from exc

    out_dir = _job_artifact_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    draft_docx = out_dir / "01_working_draft_v1.docx"
    draft_docx.write_bytes(raw_bytes)

    # Invalidate stale downstream roundtrip artifacts (04..06) when a new working draft is uploaded
    for stale_name in (
        "04_revised_clean_v2.docx",
        "05_tracked_changes_redline_v1_to_v2.docx",
        "06_resubmission_package.zip",
    ):
        stale_path = out_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    parsed_docx = parse_docx_working_document(draft_docx)

    body_text = parsed_docx["body_text"] or "\n".join(parsed_docx["table_cells"])
    pv_result = genie.invoke_tool(
        "screen_pharmacovigilance_cv08",
        job=job,
        intake_text=body_text,
        router=router,
    )
    if pv_result.get("cv08_flagged"):
        job.state = JobLifecycleState.HOLD
        JOBS_DB.save_job(job)
        return {
            "job_id": job.job_id,
            "tenant_id": caller_tenant,
            "original_filename": file.filename or "01_working_draft_v1.docx",
            "artifact_filename": "01_working_draft_v1.docx",
            "download_url": f"/jobs/{job.job_id}/artifacts/01_working_draft_v1.docx",
            "parsed_docx": parsed_docx,
            "pv_screening": pv_result,
            "artifacts": _collect_job_artifacts_metadata(job_id),
            "job": job.model_dump(),
        }

    try:
        genie.invoke_tool(
            "ingest_working_document",
            job=job,
            body_text=body_text,
            actor=actor,
            event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
        )
        genie.invoke_tool(
            "validate_strategy",
            job=job,
            text_to_check=body_text,
        )
        for cmt in job.comments:
            if cmt.human_decision == "accepted" and cmt.placement_status == "placed":
                cmt.placement_status = "pending"

        ref_pdf = out_dir / "02_reference_source.pdf"
        annotated_pdf = out_dir / "03_annotated_reference.pdf"
        if ref_pdf.exists() and job.metadata.get("uploaded_reference_pdf"):
            genie.invoke_tool(
                "locate_and_anchor_evidence",
                job=job,
                required_mandate_action="prepare_annotations",
                source_pack=job.metadata["uploaded_reference_pdf"].get("source_pack", []),
                router=router,
            )
            job.metadata["last_annotated_pdf"] = render_annotated_reference_pdf(
                ref_pdf, annotated_pdf, claims=job.claims
            )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    job.metadata["uploaded_working_docx"] = {
        **parsed_docx,
        "original_filename": file.filename or "01_working_draft_v1.docx",
    }
    JOBS_DB.save_job(job)
    return {
        "job_id": job.job_id,
        "tenant_id": caller_tenant,
        "original_filename": file.filename or "01_working_draft_v1.docx",
        "artifact_filename": "01_working_draft_v1.docx",
        "download_url": f"/jobs/{job.job_id}/artifacts/01_working_draft_v1.docx",
        "parsed_docx": parsed_docx,
        "pv_screening": pv_result,
        "artifacts": _collect_job_artifacts_metadata(job_id),
        "job": job.model_dump(),
    }


@app.post("/jobs/{job_id}/upload-reference-pdf")
@app.post("/jobs/{job_id}/files/reference-pdf")
def upload_reference_pdf_endpoint(
    job_id: str,
    request: Request,
    file: UploadFile = File(...),
    source_id: str = Form("SRC-UPLOADED-PDF"),
    version: str = Form("1.0"),
) -> dict[str, Any]:
    """
    Accepts a real `.pdf` reference pack upload from the browser, saves it as
    `02_reference_source.pdf`, extracts exact PyMuPDF 2D bounding boxes and text blocks,
    anchors job claims, and renders `03_annotated_reference.pdf` with highlight overlays.
    """
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, _ = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    raw_bytes = file.file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded .pdf file is empty.")

    try:
        import pymupdf

        check_doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
        if len(check_doc) == 0:
            check_doc.close()
            raise ValueError("PDF has 0 pages")
        check_doc.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid .pdf file: {exc}") from exc

    out_dir = _job_artifact_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_pdf = out_dir / "02_reference_source.pdf"
    annotated_pdf = out_dir / "03_annotated_reference.pdf"
    ref_pdf.write_bytes(raw_bytes)

    pdf_inspection = inspect_pdf_document_and_extract_source_pack(
        ref_pdf,
        source_id=source_id,
        version=version,
    )

    context_cache_meta = router.create_reference_context_cache(
        cache_key=f"{job.tenant_id}:{job.job_id}:{source_id}:{version}",
        pdf_or_text_content=pdf_inspection["source_pack"],
        ttl="3600s",
    )
    job.metadata["gemini_context_cache"] = context_cache_meta

    try:
        evidence_anchoring = genie.invoke_tool(
            "locate_and_anchor_evidence",
            job=job,
            required_mandate_action="prepare_annotations",
            source_pack=pdf_inspection["source_pack"],
            router=router,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    annot_meta = render_annotated_reference_pdf(ref_pdf, annotated_pdf, claims=job.claims)
    job.metadata["uploaded_reference_pdf"] = {
        "original_filename": file.filename or "02_reference_source.pdf",
        "source_id": source_id,
        "version": version,
        "file_sha256": pdf_inspection["file_sha256"],
        "page_count": pdf_inspection["page_count"],
        "pages": pdf_inspection["pages"],
        "source_pack": pdf_inspection["source_pack"],
        "gemini_context_cache": context_cache_meta,
    }
    job.metadata["last_annotated_pdf"] = annot_meta
    JOBS_DB.save_job(job)

    return {
        "job_id": job.job_id,
        "tenant_id": caller_tenant,
        "original_filename": file.filename or "02_reference_source.pdf",
        "artifact_filename": "02_reference_source.pdf",
        "download_url": f"/jobs/{job.job_id}/artifacts/02_reference_source.pdf",
        "annotated_download_url": f"/jobs/{job.job_id}/artifacts/03_annotated_reference.pdf",
        "reference_pdf": pdf_inspection,
        "gemini_context_cache": context_cache_meta,
        "evidence_anchoring": [c.model_dump() for c in evidence_anchoring],
        "annotated_pdf": annot_meta,
        "artifacts": _collect_job_artifacts_metadata(job_id),
        "job": job.model_dump(),
    }


@app.post("/jobs/{job_id}/execute-file-roundtrip")
def execute_file_roundtrip_endpoint(job_id: str, request: Request) -> dict[str, Any]:
    """
    Executes a complete file-to-file MLR review round on disk using python-docx and PyMuPDF:
    1. Writes/ingests working draft .docx (or uses uploaded 01_working_draft_v1.docx) and born-digital/uploaded reference .pdf
    2. Extracts PyMuPDF coordinates and renders annotated reference .pdf
    3. Imports and resolves reviewer comments, writes clean revised .docx + OpenXML Track Changes redline .docx
    4. Bundles manifest-verified resubmission .zip
    """
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, actor = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    out_dir = _job_artifact_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    draft_docx = out_dir / "01_working_draft_v1.docx"
    ref_pdf = out_dir / "02_reference_source.pdf"
    annotated_pdf = out_dir / "03_annotated_reference.pdf"
    clean_docx = out_dir / "04_revised_clean_v2.docx"
    redline_docx = out_dir / "05_tracked_changes_redline_v1_to_v2.docx"
    package_zip = out_dir / "06_resubmission_package.zip"

    if draft_docx.exists() and job.metadata.get("uploaded_working_docx"):
        parsed_docx = parse_docx_working_document(draft_docx)
    else:
        initial_paragraphs = [
            p.strip()
            for p in (job.revisions[0].body if job.revisions else "").split("\n")
            if p.strip()
        ]
        if not initial_paragraphs:
            initial_paragraphs = [
                "In 2L+ EGFR-mutated NSCLC, Aurenix reduced progression-free survival risk by 38% (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
                "Important Safety Information: Aurenix is contraindicated in patients with severe hepatic impairment.",
            ]
        create_working_draft_docx(draft_docx, title=job.title, paragraphs=initial_paragraphs)
        parsed_docx = parse_docx_working_document(draft_docx)
        if not job.revisions:
            ingest_working_document(
                job=job,
                body_text=parsed_docx["body_text"],
                actor=actor,
                event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
            )

    existing_source_entries = list(
        job.source_pack.get("entries", [])
        if isinstance(job.source_pack, dict)
        else job.source_pack
    )

    apply_accepted_changes(job=job, comments=job.comments, human_actor=actor)

    if ref_pdf.exists() and job.metadata.get("uploaded_reference_pdf"):
        pdf_inspection = inspect_pdf_document_and_extract_source_pack(
            ref_pdf,
            source_id=str(job.metadata["uploaded_reference_pdf"].get("source_id", "SRC-UPLOADED-PDF")),
            version=str(job.metadata["uploaded_reference_pdf"].get("version", "1.0")),
        )
        pdf_pack = pdf_inspection["source_pack"]
    elif existing_source_entries:
        source_passages: list[str] = []
        passage_rules: list[dict[str, Any]] = []
        for entry in existing_source_entries:
            verbatim = str(entry.get("verbatim_text", "")).strip()
            if verbatim and verbatim not in source_passages:
                source_passages.append(verbatim)
            words = [w.lower() for w in re.findall(r"[a-zA-Z0-9%.+-]+", verbatim) if len(w) >= 4]
            nums = re.findall(r"(?<![\d.])\d+(?:\.\d+)?%?(?!\d)", verbatim)
            passage_rules.append(
                {
                    "verbatim_text": verbatim,
                    "search_probe": str(entry.get("search_probe") or verbatim[:55]).strip(),
                    "keywords": list(entry.get("keywords") or dict.fromkeys(words[:8] + nums[:4])),
                    "locked_numbers": list(
                        entry.get("locked_numbers") or [n for n in nums if n.endswith("%") or "." in n]
                    ),
                    "required_qualifiers": list(entry.get("required_qualifiers") or []),
                    "ruled_out_terms": list(entry.get("ruled_out_terms") or []),
                    "device_version": entry.get("device_version"),
                }
            )
        if not source_passages:
            source_passages = [
                "Important Safety Information: Aurenix is contraindicated in patients with severe hepatic impairment."
            ]
        pages_spec = []
        for idx in range(0, len(source_passages), 5):
            chunk = source_passages[idx : idx + 5]
            page_no = (idx // 5) + 1
            pages_spec.append(
                {
                    "header": f"Authorized Reference Source Pack — Page {page_no}",
                    "passages": chunk,
                }
            )
        create_born_digital_reference_pdf(ref_pdf, pages_spec=pages_spec)
        primary_source_id = str(existing_source_entries[0].get("source_id", "SRC-AURORA-301-PDF"))
        primary_version = str(existing_source_entries[0].get("version", "2.0"))
        extracted_pack = extract_pdf_source_pack(
            ref_pdf,
            source_id=primary_source_id,
            version=primary_version,
            passage_rules=passage_rules,
        )
        pdf_pack = []
        for orig_entry, ext_entry in zip(existing_source_entries, extracted_pack):
            merged = dict(orig_entry)
            merged.update(ext_entry)
            if "source_id" in orig_entry:
                merged["source_id"] = orig_entry["source_id"]
            if "version" in orig_entry:
                merged["version"] = orig_entry["version"]
            pdf_pack.append(merged)
    else:
        default_passages = [
            "In 2L+ EGFR-mutated NSCLC, Aurenix reduced progression-free survival risk by 38% (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
            "Important Safety Information: Aurenix is contraindicated in patients with severe hepatic impairment.",
        ]
        passage_rules = [
            {
                "verbatim_text": "In 2L+ EGFR-mutated NSCLC, Aurenix reduced progression-free survival risk by 38% (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
                "search_probe": "reduced progression-free survival risk by 38%",
                "keywords": ["aurenix", "38%", "progression-free"],
                "locked_numbers": ["38%", "0.62", "0.49", "0.78"],
                "required_qualifiers": ["2L+ EGFR-mutated NSCLC", "95% CI"],
            }
        ]
        # Include sentences from both the base working draft and the active post-edit revision for unseeded demo jobs
        latest_body = job.revisions[-1].body if job.revisions else parsed_docx["body_text"]
        doc_sentences = list(
            dict.fromkeys(
                split_medical_sentences(parsed_docx["body_text"])
                + split_medical_sentences(latest_body)
            )
        )
        for sent in doc_sentences:
            if sent not in default_passages and not sent.lower().startswith("important safety information"):
                default_passages.append(sent)
                words = [w.lower() for w in re.findall(r"[a-zA-Z0-9%.+-]+", sent) if len(w) >= 4]
                nums = re.findall(r"(?<![\d.])\d+(?:\.\d+)?%?(?!\d)", sent)
                passage_rules.append(
                    {
                        "verbatim_text": sent,
                        "search_probe": sent[:55].strip(),
                        "keywords": list(dict.fromkeys(words[:8] + nums[:4])),
                        "locked_numbers": [n for n in nums if n.endswith("%") or "." in n],
                        "required_qualifiers": [],
                    }
                )

        # Split across pages if more than 5 passages so textboxes fit within 792pt page height
        pages_spec = []
        for idx in range(0, len(default_passages), 5):
            chunk = default_passages[idx : idx + 5]
            page_no = (idx // 5) + 1
            pages_spec.append(
                {
                    "header": f"AURORA-301 Clinical Study Report & USPI Reference — Page {page_no}",
                    "passages": chunk,
                }
            )
        create_born_digital_reference_pdf(ref_pdf, pages_spec=pages_spec)
        pdf_pack = extract_pdf_source_pack(
            ref_pdf,
            source_id="SRC-AURORA-301-PDF",
            version="2.0",
            passage_rules=passage_rules,
        )

    if "gemini_context_cache" not in job.metadata:
        job.metadata["gemini_context_cache"] = router.create_reference_context_cache(
            cache_key=f"{job.tenant_id}:{job.job_id}:SRC-AURORA-301-PDF:2.0",
            pdf_or_text_content=pdf_pack,
            ttl="3600s",
        )

    locate_and_anchor_evidence(job=job, source_pack=pdf_pack, router=router)
    annot_meta = render_annotated_reference_pdf(ref_pdf, annotated_pdf, claims=job.claims)

    clean_meta = write_clean_revised_docx(draft_docx, clean_docx, accepted_comments=job.comments)
    redline_meta = write_tracked_changes_redline_docx(
        draft_docx,
        redline_docx,
        accepted_comments=job.comments,
    )
    zip_meta = build_resubmission_package_zip(
        job=job,
        original_docx_path=draft_docx,
        original_pdf_path=ref_pdf,
        clean_revised_docx_path=clean_docx,
        redline_docx_path=redline_docx,
        annotated_pdf_path=annotated_pdf,
        output_zip_path=package_zip,
    )
    artifacts_meta = _collect_job_artifacts_metadata(job_id)
    job.metadata["last_roundtrip_artifacts"] = artifacts_meta
    job.metadata["last_annotated_pdf"] = annot_meta
    JOBS_DB.save_job(job)
    return {
        "job_id": job.job_id,
        "tenant_id": caller_tenant,
        "parsed_docx": parsed_docx,
        "annotated_pdf": annot_meta,
        "clean_revised_docx": clean_meta,
        "redline_docx": redline_meta,
        "resubmission_zip": zip_meta,
        "artifacts": artifacts_meta,
        "job": job.model_dump(),
    }


_execute_roundtrip_for_job = execute_file_roundtrip_endpoint


@app.get("/jobs/{job_id}/artifacts")
def list_job_artifacts(job_id: str, request: Request) -> dict[str, Any]:
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, _ = _require_authenticated_tenant(request, expected_tenant_id=job.tenant_id)
    try:
        job = JOBS_DB.get_for_tenant(job_id, caller_tenant) or job
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "job_id": job.job_id,
        "tenant_id": caller_tenant,
        "artifacts": _collect_job_artifacts_metadata(job_id),
        "annotated_pdf": job.metadata.get("last_annotated_pdf"),
    }


@app.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_job_artifact(
    job_id: str,
    artifact_name: str,
    request: Request,
) -> FileResponse:
    """
    One-click browser download endpoint for the 6 generated MLR roundtrip artifacts:
    `01_working_draft_v1.docx`, `02_reference_source.pdf`, `03_annotated_reference.pdf`,
    `04_revised_clean_v2.docx`, `05_tracked_changes_redline_v1_to_v2.docx`, and
    `06_resubmission_package.zip`.
    """
    media_type = ALLOWED_ARTIFACT_MEDIA_TYPES.get(artifact_name)
    if not media_type:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown or disallowed artifact filename: '{artifact_name}'.",
        )
    job = JOBS_DB.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    caller_tenant, _ = _require_authenticated_tenant(
        request,
        expected_tenant_id=job.tenant_id,
        allow_query_auth=True,
    )
    try:
        _ = JOBS_DB.get_for_tenant(job_id, caller_tenant)
    except CrossTenantAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    file_path = _job_artifact_dir(job_id) / artifact_name
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{artifact_name}' has not been generated yet for job '{job_id}'.",
        )
    return FileResponse(
        path=str(file_path),
        filename=artifact_name,
        media_type=media_type,
    )


@app.get("/evals/run-12-cases")
def run_acceptance_evals() -> dict[str, Any]:
    suite_results = run_all_12_acceptance_cases()
    all_passed = all(item.get("passed", False) for item in suite_results.values())
    return {
        "all_12_passed": all_passed,
        "total_cases": len(suite_results),
        "cases": suite_results,
    }


@app.get("/evals/run-pharma-comms-battery")
def run_pharma_comms_battery_endpoint() -> dict[str, Any]:
    battery_results = run_pharma_comms_scenario_battery()
    all_passed = all(item.get("passed", False) for item in battery_results.values())
    return {
        "all_15_passed": all_passed,
        "total_scenarios": len(battery_results),
        "scenarios": battery_results,
    }
