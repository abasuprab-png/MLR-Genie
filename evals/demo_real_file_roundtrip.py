from __future__ import annotations

from pathlib import Path
from typing import Any
import zipfile

from backend.document_engine import (
    build_resubmission_package_zip,
    create_born_digital_reference_pdf,
    create_working_draft_docx,
    extract_pdf_source_pack,
    parse_docx_working_document,
    render_annotated_reference_pdf,
    write_clean_revised_docx,
    write_tracked_changes_redline_docx,
)
from backend.domain.schemas import (
    CommentCategory,
    JobRecord,
    ProvenanceEventType,
    ReviewComment,
    SectorType,
    SupportClassification,
    WordingSpanEdit,
)
from backend.tools.document_tools import (
    apply_accepted_changes,
    ingest_working_document,
    keep_source,
    locate_and_anchor_evidence,
)
from backend.tools.review_tools import import_and_reconcile_comments


def run_real_file_to_file_mlr_round(output_dir: Path | None = None) -> dict[str, Any]:
    """
    Executes a complete, verifiable file-to-file MLR review round on disk:
    1. Creates & parses working draft .docx (`python-docx`)
    2. Creates born-digital reference .pdf & extracts exact coordinates (`PyMuPDF` / `pymupdf`)
    3. Anchors claims & renders annotated reference .pdf with visual highlight overlays
    4. Imports Medical, Legal, and Regulatory comments (accepted edit, kept_source, conflict escalation)
    5. Applies accepted changes atomically to produce clean revised .docx + OpenXML <w:del>/<w:ins> redline .docx
    6. Bundles and verifies the complete resubmission package .zip with SHA-256 manifest
    """
    base_dir = output_dir or (
        Path(__file__).resolve().parent.parent / "artifacts" / "file_roundtrip" / "job_demo_aurenix_round_1"
    )
    base_dir.mkdir(parents=True, exist_ok=True)

    draft_docx = base_dir / "01_working_draft_v1.docx"
    ref_pdf = base_dir / "02_reference_source.pdf"
    annotated_pdf = base_dir / "03_annotated_reference.pdf"
    clean_docx = base_dir / "04_revised_clean_v2.docx"
    redline_docx = base_dir / "05_tracked_changes_redline_v1_to_v2.docx"
    package_zip = base_dir / "06_resubmission_package.zip"

    job = JobRecord(
        job_id="job_demo_aurenix_round_1",
        tenant_id="tenant_aurenix",
        title="Aurenix Phase 3 AURORA-301 Press Release (Round 1 -> Round 2 Resubmission)",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Press Release",
        destination_platform="package_mode",
        deadline_iso="2026-11-05T17:00:00Z",
    )

    draft_paragraphs = [
        "In 2L+ EGFR-mutated NSCLC, Aurenix® reduced progression-free survival risk by 38%† (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
        "Median follow-up was 14.2 months across 410 randomized patients in the AURORA-301 study.",
        "Important Safety Information: Aurenix® is contraindicated in patients with severe hepatic impairment; monitor hepatic transaminases prior to initiation.",
    ]

    # Step 1: Create and parse working draft .docx with inline <w:rPr> formatting (bold, italic, superscript ® and †)
    create_working_draft_docx(draft_docx, title=job.title, paragraphs=draft_paragraphs)
    import docx as _docx

    _doc = _docx.Document(str(draft_docx))
    _p0 = _doc.paragraphs[1]
    _p0.text = ""
    _p0.add_run("In 2L+ EGFR-mutated NSCLC, ")
    _r_brand = _p0.add_run("Aurenix")
    _r_brand.bold = True
    _r_reg = _p0.add_run("®")
    _r_reg.font.superscript = True
    _p0.add_run(" reduced progression-free survival risk by 38%")
    _r_dag = _p0.add_run("†")
    _r_dag.font.superscript = True
    _p0.add_run(" (HR 0.62; 95% CI, 0.49 to 0.78; ")
    _r_pval = _p0.add_run("p<0.001")
    _r_pval.italic = True
    _p0.add_run(").")

    _p2 = _doc.paragraphs[3]
    _p2.text = ""
    _r_isi = _p2.add_run("Important Safety Information:")
    _r_isi.bold = True
    _p2.add_run(" ")
    _r_brand2 = _p2.add_run("Aurenix")
    _r_brand2.bold = True
    _r_reg2 = _p2.add_run("®")
    _r_reg2.font.superscript = True
    _p2.add_run(" is contraindicated in patients with severe hepatic impairment; monitor hepatic transaminases prior to initiation.")
    _doc.save(str(draft_docx))

    parsed_v1 = parse_docx_working_document(draft_docx)
    ingest_working_document(
        job=job,
        body_text=parsed_v1["body_text"],
        actor="sarah.chen@aurenix.example",
        event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
    )

    # Step 2: Create born-digital reference .pdf and extract real PyMuPDF bounding boxes
    create_born_digital_reference_pdf(
        ref_pdf,
        pages_spec=[
            {
                "header": "AURORA-301 Clinical Study Report & Approved USPI Reference — Page 1",
                "passages": [
                    "In 2L+ EGFR-mutated NSCLC, Aurenix® reduced progression-free survival risk by 38%† (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
                    "Median follow-up was 14.2 months across 410 randomized patients in the AURORA-301 study.",
                    "Important Safety Information: Aurenix® is contraindicated in patients with severe hepatic impairment; monitor hepatic transaminases prior to initiation.",
                ],
            },
            {
                "header": "AURORA-301 Clinical Reprint (Two-Column Layout) — Page 2",
                "left_column": [
                    "Study Design (AURORA-301): Randomized, double-blind, placebo-controlled Phase 3 study in 2L+ EGFR-mutated NSCLC.",
                    "Primary Endpoint: Progression-free survival (PFS) assessed by blinded independent central review (BICR).",
                ],
                "right_column": [
                    "Epidemiology & Disease Burden: NSCLC accounts for approximately 85% of all lung cancers in adults.",
                    "Hepatic Safety Monitoring: Evaluate ALT, AST, and total bilirubin prior to starting Aurenix® and periodically thereafter.",
                ],
            },
        ],
    )
    source_pack = extract_pdf_source_pack(
        ref_pdf,
        source_id="SRC-AURORA-301-CSR",
        version="2.0",
        passage_rules=[
            {
                "verbatim_text": "In 2L+ EGFR-mutated NSCLC, Aurenix® reduced progression-free survival risk by 38%† (HR 0.62; 95% CI, 0.49 to 0.78; p<0.001).",
                "search_probe": "reduced progression-free survival risk by 38%",
                "keywords": ["38%", "progression-free survival"],
                "locked_numbers": ["38%", "0.62", "0.49", "0.78"],
                "required_qualifiers": ["2L+ EGFR-mutated NSCLC", "95% CI"],
            },
            {
                "verbatim_text": "Median follow-up was 14.2 months across 410 randomized patients in the AURORA-301 study.",
                "search_probe": "Median follow-up was 14.2 months",
                "keywords": ["14.2 months", "410 randomized"],
                "locked_numbers": ["14.2", "410"],
                "required_qualifiers": ["AURORA-301"],
            },
        ],
    )

    # Step 3: Verify claims and render annotated reference PDF with highlight boxes
    claims = locate_and_anchor_evidence(job=job, source_pack=source_pack)
    annotated_pdf_meta = render_annotated_reference_pdf(ref_pdf, annotated_pdf, claims=claims)

    # Step 4: Import reviewer comments and record human decisions
    comments = [
        ReviewComment(
            comment_id="cmt_reg_01",
            reviewer_name="Dr. David Miller",
            reviewer_role="Regulatory",
            timestamp="2026-10-28T14:10:00Z",
            anchored_passage=draft_paragraphs[0],
            comment_text="Specify 'statistically significant' before '38%†' per Section 14.1 while preserving dagger footnote superscript.",
            category=CommentCategory.FACTUAL_CORRECTION,
            human_decision="accepted",
            proposed_spans=[
                WordingSpanEdit(
                    target_text="reduced progression-free survival risk by 38%†",
                    replacement_text="achieved a statistically significant 38%† reduction in progression-free survival risk",
                )
            ],
        ),
        ReviewComment(
            comment_id="cmt_med_02",
            reviewer_name="Dr. Elena Rostova",
            reviewer_role="Medical",
            timestamp="2026-10-28T14:22:00Z",
            anchored_passage=draft_paragraphs[1],
            comment_text="Optional editorial suggestion to abbreviate AURORA-301.",
            category=CommentCategory.EDITORIAL_REQUEST,
            human_decision="rejected",
            proposed_spans=[
                WordingSpanEdit(
                    target_text="in the AURORA-301 study.",
                    replacement_text="in Study AUR-301.",
                )
            ],
        ),
    ]
    import_and_reconcile_comments(job=job, raw_comments=comments)
    keep_source(job=job, comment=job.comments[1], human_actor="sarah.chen@aurenix.example")
    apply_accepted_changes(
        job=job,
        comments=job.comments,
        human_actor="sarah.chen@aurenix.example",
    )

    # Re-verify claims on post-edit v2 revision and re-render annotated reference PDF
    claims = locate_and_anchor_evidence(job=job, source_pack=source_pack)
    annotated_pdf_meta = render_annotated_reference_pdf(ref_pdf, annotated_pdf, claims=claims)

    # Step 5: Write clean revised .docx and genuine OpenXML Track Changes redline .docx
    clean_docx_meta = write_clean_revised_docx(
        draft_docx,
        clean_docx,
        accepted_comments=job.comments,
    )
    redline_docx_meta = write_tracked_changes_redline_docx(
        draft_docx,
        redline_docx,
        accepted_comments=job.comments,
    )

    # Verify OpenXML <w:del> and <w:ins> elements exist inside word/document.xml of redline_docx
    with zipfile.ZipFile(redline_docx, "r") as zf:
        doc_xml = zf.read("word/document.xml").decode("utf-8")
    has_w_del = "<w:del " in doc_xml or "<w:del>" in doc_xml
    has_w_ins = "<w:ins " in doc_xml or "<w:ins>" in doc_xml

    # Step 6: Build manifest-verified resubmission ZIP package
    zip_meta = build_resubmission_package_zip(
        job=job,
        original_docx_path=draft_docx,
        original_pdf_path=ref_pdf,
        clean_revised_docx_path=clean_docx,
        redline_docx_path=redline_docx,
        annotated_pdf_path=annotated_pdf,
        output_zip_path=package_zip,
    )

    return {
        "passed": (
            all(c.support_status == SupportClassification.SUPPORTED for c in claims)
            and all(c.bound_version == job.current_version and not c.stale for c in claims)
            and all(c.anchors[0].anchor_valid is True for c in claims)
            and annotated_pdf_meta["annotation_count"] == 2
            and redline_docx_meta["tracked_replacements"] == 1
            and has_w_del is True
            and has_w_ins is True
            and package_zip.exists()
        ),
        "output_directory": str(base_dir),
        "claims_verified": len(claims),
        "annotated_pdf": annotated_pdf_meta,
        "clean_revised_docx": clean_docx_meta,
        "redline_docx": {
            **redline_docx_meta,
            "openxml_has_w_del": has_w_del,
            "openxml_has_w_ins": has_w_ins,
        },
        "resubmission_zip": zip_meta,
    }


if __name__ == "__main__":
    import json
    import sys

    summary = run_real_file_to_file_mlr_round()
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["passed"] else 1)
