"""Thin integration orchestration over the existing review and file pipelines."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import textwrap
from typing import Callable
from uuid import uuid4

import docx
import pymupdf
from fastapi import HTTPException

from backend.agent.gemini_gateway import GeminiGateway, ModelAssessmentError
from backend.agent.models_router import GeminiModelRouter
from backend.document_engine import inspect_pdf_document_and_extract_source_pack, parse_docx_working_document, sha256_file
from backend.domain.integrations import (
    CamsSourceAnchor, CamsVerificationInput, CamsVerificationResponse,
    CommsRxRoundtripInput, CommsRxRoundtripResponse, SourceMetadata,
)
from backend.domain.schemas import JobRecord, ReviewComment, SectorType
from backend.storage import DurableJobStore
from backend.tools.document_tools import ingest_working_document, locate_and_anchor_evidence
from backend.tools.review_tools import import_and_reconcile_comments, screen_pharmacovigilance_cv08


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
COMMSRX_ARTIFACTS = {
    "redline.docx": ("05_tracked_changes_redline_v1_to_v2.docx", DOCX_MEDIA_TYPE),
    "clean_revised.docx": ("04_revised_clean_v2.docx", DOCX_MEDIA_TYPE),
    "audit_summary.pdf": ("07_audit_summary.pdf", "application/pdf"),
    "annotated_source.pdf": ("03_annotated_reference.pdf", "application/pdf"),
}
MAX_REFERENCE_BYTES = 32 * 1024 * 1024


def cams_verdict(status: str) -> str:
    return status if status in ("supported", "contradicted") else "insufficient"


def validate_reference_pdf(data: bytes) -> None:
    if not data or len(data) > MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=413 if data else 400, detail="Reference PDF must contain 1–33554432 bytes.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass or len(pdf) == 0:
                raise ValueError("Locked or empty PDF")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Reference must be a readable, unencrypted PDF.") from exc


def write_audit_summary(job: JobRecord, output: Path) -> None:
    """A separate, paginated review audit; never relabel a reference PDF as an audit."""
    lines = ["MLR Genie review audit", f"Job: {job.job_id}", f"State: {job.state.value}",
             f"Generated: {datetime.now(timezone.utc).isoformat()}",
             "Review evidence and human decisions; this report is not regulatory approval.", ""]
    for claim in job.claims:
        lines += [f"Claim {claim.claim_id}: {claim.support_status.value}", claim.sentence_text,
                  claim.plain_english_diagnosis]
        lines += [f"Source {a.source_document_id}, page {a.page_number}: {a.verbatim_quote}" for a in claim.anchors]
    lines += ["", "Reviewer decisions"]
    for comment in job.comments:
        lines += [f"{comment.comment_id}: {comment.reviewer_name} ({comment.reviewer_role})",
                  comment.comment_text,
                  f"Decision: {comment.human_decision}; placement: {comment.placement_status}; actor: {comment.human_actor}"]
        lines += [f"Edit: {s.target_text} -> {s.replacement_text}" for s in comment.proposed_spans]
    lines += ["", "Revision provenance"]
    lines += [f"Version {r.version_number}: {r.sha256_hash}; actor: {r.actor}; event: {r.event_type.value}" for r in job.revisions]
    lines += ["", "Verification transport"]
    for receipt in job.metadata.get("telemetry_receipts", []):
        lines.append(f"{receipt.get('effective_model')} / {receipt.get('transport')} / live={receipt.get('used_live_sdk')}")
    wrapped = [part for line in lines for part in (textwrap.wrap(line, width=92) or [""])]
    with pymupdf.open() as pdf:
        for offset in range(0, len(wrapped), 53):
            page = pdf.new_page()
            for index, line in enumerate(wrapped[offset:offset + 53]):
                page.insert_text((36, 45 + index * 14), line, fontsize=9)
        pdf.save(output)


class IntegrationService:
    def __init__(self, store: DurableJobStore, router: GeminiModelRouter, artifact_dir: Callable[[str], Path]):
        self.store, self.router, self.artifact_dir = store, router, artifact_dir

    def require_live_receipts(self, job: JobRecord, require_live: bool) -> None:
        if GeminiGateway(self.router, require_live=require_live).require_live:
            receipts = job.metadata.get("telemetry_receipts", [])
            if not receipts or any(not r.get("used_live_sdk") for r in receipts):
                raise ModelAssessmentError("Integration verification requires successful live Gemini calls.")

    def prepare(
        self, *, tenant: str, actor: str, body: str, claims: list[str], pdf_bytes: bytes,
        source: SourceMetadata, require_live: bool, title: str, sector: SectorType,
    ) -> JobRecord:
        validate_reference_pdf(pdf_bytes)
        if not body.strip():
            raise HTTPException(status_code=422, detail="Claim/body text must not be blank.")
        if GeminiGateway(self.router, require_live=require_live).require_live and self.router._client is None:
            raise ModelAssessmentError("Live Gemini verification required but unavailable.")
        job = JobRecord(job_id=f"integration_{uuid4().hex}", tenant_id=tenant, title=title, sector=sector,
                        document_type="Integration review", destination_platform="package_mode",
                        deadline_iso=datetime.now(timezone.utc).isoformat())
        screen = screen_pharmacovigilance_cv08(job=job, intake_text=body, router=self.router)
        if screen["cv08_flagged"]:
            self.store.create_job_strict(job)
            raise HTTPException(status_code=409, detail={"job_id": job.job_id, "state": "hold", "cv08_flagged": True})
        out = self.artifact_dir(job.job_id)
        out.mkdir(parents=True, exist_ok=True)
        reference = out / "02_reference_source.pdf"
        reference.write_bytes(pdf_bytes)
        inspection = inspect_pdf_document_and_extract_source_pack(reference, source_id=source.source_id, version=source.version)
        job.metadata["integration_source"] = source.model_dump(by_alias=True)
        job.metadata["uploaded_reference_pdf"] = {
            "source_id": source.source_id, "version": source.version, **inspection,
        }
        job.metadata["gemini_context_cache"] = self.router.create_reference_context_cache(
            cache_key=f"{tenant}:{job.job_id}:{source.source_id}:{source.version}",
            pdf_or_text_content=inspection["source_pack"],
        )
        ingest_working_document(job=job, body_text=body, actor=actor)
        locate_and_anchor_evidence(job=job, source_pack=inspection["source_pack"], router=self.router, claim_texts=claims)
        self.require_live_receipts(job, require_live)
        self.store.create_job_strict(job)
        return job

    def verify_cams(self, body: CamsVerificationInput, pdf_bytes: bytes, tenant: str, actor: str) -> CamsVerificationResponse:
        job = self.prepare(tenant=tenant, actor=actor, body=body.claim_text, claims=[body.claim_text],
                           pdf_bytes=pdf_bytes, source=body.source, require_live=body.require_live,
                           title="CAMS source verification", sector=SectorType.CORPORATE)
        claim = job.claims[0]
        receipt = job.metadata["telemetry_receipts"][0]
        engine = receipt["effective_model"] if receipt.get("used_live_sdk") else "mlr-genie:offline"
        anchor = CamsSourceAnchor(verdict=cams_verdict(claim.support_status.value),
                                  verifiedBy=f"{engine}+pymupdf", verifiedAt=datetime.now(timezone.utc))
        dimensions = None
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
            for candidate in claim.anchors:
                if not candidate.anchor_valid or candidate.page_number > len(pdf):
                    continue
                page = pdf[candidate.page_number - 1]
                rect = pymupdf.Rect(candidate.x0, candidate.y0, candidate.x1, candidate.y1)
                if rect not in page.rect:
                    continue
                quote = page.get_text("text", clip=rect + (-1, -1, 1, 1)).strip()
                if not quote or " ".join(quote.split()) != " ".join(candidate.verbatim_quote.split()):
                    continue
                anchor.page = candidate.page_number
                from backend.domain.integrations import CamsBoundingBox
                anchor.boundingBox = CamsBoundingBox(x=rect.x0, y=rect.y0, width=rect.width, height=rect.height)
                anchor.verbatimQuote = quote
                dimensions = {"width": page.rect.width, "height": page.rect.height}
                break
        if anchor.page is None and anchor.verdict == "supported":
            anchor.verdict = "insufficient"
        response = CamsVerificationResponse(job_id=job.job_id, claim_id=claim.claim_id, anchor=anchor,
                                            pageDimensions=dimensions, telemetry_receipts=job.metadata["telemetry_receipts"])
        job.metadata["cams_verification"] = response.model_dump(mode="json")
        self.store.save_job(job)
        return response

    def commsrx_roundtrip(
        self, body: CommsRxRoundtripInput, pdf_bytes: bytes, tenant: str, actor: str,
        execute: Callable[[str], dict],
    ) -> CommsRxRoundtripResponse:
        job = self.prepare(tenant=tenant, actor=actor, body=body.body_text, claims=[c.text for c in body.claims],
                           pdf_bytes=pdf_bytes, source=body.source, require_live=body.require_live,
                           title=body.title, sector=body.sector)
        job.metadata["integration_claims"] = [c.model_dump() for c in body.claims]
        accepted = {edit.comment_id: edit for edit in body.accepted_edits}
        comments = [ReviewComment(
            comment_id=c.comment_id, reviewer_name=c.reviewer_name, reviewer_role=c.reviewer_role,
            timestamp=c.timestamp.isoformat(), anchored_passage=c.anchored_passage,
            target_sentence_id=c.claim_id, comment_text=c.comment_text, category="editorial_request",
            proposed_spans=[edit.model_dump() for edit in (accepted[c.comment_id].edits if c.comment_id in accepted else c.proposed_edits)],
            human_decision="accepted" if c.comment_id in accepted else c.decision,
            human_actor=actor if c.comment_id in accepted or c.decision != "pending" else None,
        ) for c in body.comments]
        reconciliation = import_and_reconcile_comments(job=job, raw_comments=comments, router=self.router)
        self.store.save_job(job)
        if reconciliation["requires_human_resolution"]:
            raise HTTPException(status_code=409, detail={"job_id": job.job_id, "requires_human_resolution": True,
                                                       "conflict_groups": reconciliation["conflict_groups"]})
        out = self.artifact_dir(job.job_id)
        original = out / "01_working_draft_v1.docx"
        document = docx.Document()
        for paragraph in body.body_text.split("\n"):
            document.add_paragraph(paragraph)
        document.save(original)
        job.metadata["uploaded_working_docx"] = parse_docx_working_document(original)
        self.store.save_job(job)
        execute(job.job_id)
        job = self.store.get_for_tenant(job.job_id, tenant)
        self.require_live_receipts(job, body.require_live)
        unplaced = [c.comment_id for c in job.comments if c.human_decision == "accepted" and c.placement_status != "placed"]
        if unplaced:
            raise HTTPException(status_code=409, detail={"job_id": job.job_id, "unplaced_comment_ids": unplaced})
        write_audit_summary(job, out / "07_audit_summary.pdf")
        artifacts = {name: {"filename": name,
                           "download_url": f"/integrations/commsrx/jobs/{job.job_id}/artifacts/{name}",
                           "media_type": media, "bytes": (out / file).stat().st_size,
                           "sha256": sha256_file(out / file)}
                     for name, (file, media) in COMMSRX_ARTIFACTS.items()}
        result = CommsRxRoundtripResponse(job_id=job.job_id, state=job.state.value,
            claims=[{"claim_id": c.claim_id, "text": c.sentence_text, "verdict": cams_verdict(c.support_status.value)} for c in job.claims],
            artifacts=artifacts, telemetry_receipts=job.metadata["telemetry_receipts"])
        job.metadata["commsrx_roundtrip"] = result.model_dump(mode="json", by_alias=True)
        self.store.save_job(job)
        return result
