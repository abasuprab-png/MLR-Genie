from __future__ import annotations

from typing import Any

from backend.agent.mlr_genie_agent import MlrGenieAgent
from backend.agent.models_router import GeminiModelRouter
from backend.domain.schemas import (
    CommentCategory,
    ConnectorActionType,
    JobLifecycleState,
    JobRecord,
    ProvenanceEventType,
    ReuseDimensionDecision,
    ReuseDimensionDisclosure,
    ReuseDimensionEvidence,
    ReviewComment,
    SectorType,
    StatementReuseRecord,
    SupportClassification,
    WordingSpanEdit,
)
from backend.tools.document_tools import (
    apply_accepted_changes,
    ingest_working_document,
    locate_and_anchor_evidence,
    reanchor_after_reflow,
)
from backend.tools.platform_adapters import CapabilityCheckedPlatformAdapter
from backend.tools.review_tools import (
    analyze_approval_day_label_delta,
    compute_multi_asset_blast_radius,
    import_and_reconcile_comments,
    query_statement_reuse_library,
)


def run_all_12_acceptance_cases() -> dict[str, dict[str, Any]]:
    """
    Section 11 — Runs all 12 mandatory acceptance cases across Pharma, Biotech, and MedTech.
    """
    router = GeminiModelRouter()
    genie = MlrGenieAgent(router=router)
    genie.register_tool("ingest_working_document", ingest_working_document)
    genie.register_tool("locate_and_anchor_evidence", locate_and_anchor_evidence)
    genie.register_tool("apply_accepted_changes", apply_accepted_changes)
    genie.register_tool("reanchor_after_reflow", reanchor_after_reflow)
    genie.register_tool("import_and_reconcile_comments", import_and_reconcile_comments)

    results: dict[str, dict[str, Any]] = {}

    # Shared Synthetic Source Pack (Pharma, Biotech, MedTech)
    source_pack = [
        {
            "source_id": "SRC-CSR-301",
            "version": "2.0",
            "keywords": ["progression-free survival", "38%", "48%", "aurenix"],
            "verbatim_text": (
                "In the Phase 3 AURORA-301 study of 2L+ EGFR-mutated NSCLC patients, "
                "Aurenix reduced the risk of disease progression or death by 38% "
                "(HR 0.62; 95% CI, 0.49 to 0.78; p<0.001)."
            ),
            "locked_numbers": ["38%", "0.62", "0.49", "0.78"],
            "required_qualifiers": ["2L+ EGFR-mutated NSCLC", "95% CI, 0.49 to 0.78"],
            "ruled_out_terms": ["all lung cancer patients", "cured"],
            "bbox": {"page": 4, "x0": 72.0, "y0": 210.0, "x1": 540.0, "y1": 265.0},
        },
        {
            "source_id": "SRC-IFU-MEDTECH-42",
            "version": "4.2",
            "keywords": ["cardiopulse", "ablation catheter", "94.2%"],
            "verbatim_text": (
                "The CardioPulse v4.2 Pulsed Field Ablation Catheter is 510(k) cleared for paroxysmal "
                "atrial fibrillation, demonstrating 94.2% acute pulmonary vein isolation."
            ),
            "locked_numbers": ["94.2%"],
            "required_qualifiers": ["510(k) cleared", "paroxysmal atrial fibrillation"],
            "device_version": "v4.2",
            "ruled_out_terms": ["fda approved for all arrhythmias"],
            "bbox": {"page": 2, "x0": 68.0, "y0": 120.0, "x1": 530.0, "y1": 175.0},
        },
    ]

    # Case 1: Missing reference
    job1 = JobRecord(
        job_id="job_case_01",
        tenant_id="tenant_onco_biotech",
        title="Biotech Congress Abstract",
        sector=SectorType.BIOTECH,
        document_type="Congress Abstract",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T17:00:00Z",
    )
    ingest_working_document(
        job=job1,
        body_text="Biomarker subgroup B achieved an 81% complete metabolic response at 24 months.",
        actor="sarah.chen@onco.example",
    )
    claims1 = locate_and_anchor_evidence(job=job1, source_pack=source_pack, router=router)
    results["case_01_missing_reference"] = {
        "passed": claims1[0].support_status == SupportClassification.UNRESOLVED
        and len(claims1[0].anchors) == 0,
        "status": claims1[0].support_status.value,
        "diagnosis": claims1[0].plain_english_diagnosis,
    }

    # Case 2: Evidence contradicting a sentence (changed 38% to 48%)
    job2 = JobRecord(
        job_id="job_case_02",
        tenant_id="tenant_onco_biotech",
        title="Phase 3 Readout Press Release",
        sector=SectorType.PHARMA_PREAPPROVAL,
        document_type="Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T17:00:00Z",
    )
    ingest_working_document(
        job=job2,
        body_text=(
            "In 2L+ EGFR-mutated NSCLC, Aurenix reduced progression-free survival risk by 48% "
            "(HR 0.62; 95% CI, 0.49 to 0.78)."
        ),
        actor="sarah.chen@onco.example",
    )
    claims2 = locate_and_anchor_evidence(job=job2, source_pack=source_pack, router=router)
    results["case_02_contradicted_sentence"] = {
        "passed": claims2[0].support_status == SupportClassification.CONTRADICTED
        and "38%" in claims2[0].plain_english_diagnosis,
        "status": claims2[0].support_status.value,
        "diagnosis": claims2[0].plain_english_diagnosis,
    }

    # Case 3: Expired or context-inappropriate prior approval (4-dimension reuse library)
    library = [
        StatementReuseRecord(
            statement_id="stmt_101",
            tenant_id="tenant_onco_biotech",
            exact_wording="Aurenix reduced the risk of disease progression or death by 38% in 2L+ EGFR-mutated NSCLC.",
            source_document_id="SRC-CSR-301",
            source_version="2.0",
            source_location="Page 4, Table 2",
            originating_material_id="MAT-2025-88",
            reviewer_role="Medical & Regulatory",
            reviewer_rationale="Cleared for US Scientific Congress Deck only.",
            product_or_device_version="Aurenix-100mg",
            sector=SectorType.PHARMA_PREAPPROVAL,
            jurisdiction="US",
            audience="HCP",
            channel="Congress Deck",
            purpose="Scientific Exchange",
            qualifications=["95% CI, 0.49 to 0.78"],
            valid_from="2025-01-01",
            valid_until="2026-06-01",  # Expired before 2026-10-28!
            evidence_support=ReuseDimensionEvidence.VERIFIED_SOURCE,
            public_disclosure=ReuseDimensionDisclosure.PUBLIC_COMPANY_FOOTPRINT,
            review_decision=ReuseDimensionDecision.CLEARED_IN_CONTEXT,
            permitted_contexts=["US:HCP:Congress Deck"],
        )
    ]
    reuse_check = query_statement_reuse_library(
        library=library,
        requesting_tenant_id="tenant_onco_biotech",
        query_text="Aurenix reduced the risk of disease progression by 38%",
        target_jurisdiction="EU",
        target_audience="Patient",
        target_channel="Social Post",
        target_product_version="Aurenix-100mg",
        as_of_date="2026-10-28",
    )
    results["case_03_expired_or_inappropriate_reuse"] = {
        "passed": reuse_check[0]["can_reuse_directly"] is False
        and len(reuse_check[0]["reasons_requiring_rereview"]) >= 2,
        "reasons": reuse_check[0]["reasons_requiring_rereview"],
    }

    # Case 4: Two conflicting reviewer comments escalated to human decision-maker
    job4 = JobRecord(
        job_id="job_case_04",
        tenant_id="tenant_onco_biotech",
        title="Executive Media Q&A",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Media Q&A",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T17:00:00Z",
    )
    conflicting_comments = [
        ReviewComment(
            comment_id="cmt_med_1",
            reviewer_name="Dr. Elena Rostova",
            reviewer_role="Medical",
            timestamp="2026-10-28T09:00:00Z",
            anchored_passage="Aurenix is well tolerated across patient groups.",
            comment_text="Specify Grade >=3 adverse events occurred in 22% of patients.",
            category=CommentCategory.FACTUAL_CORRECTION,
            proposed_spans=[
                WordingSpanEdit(
                    target_text="Aurenix is well tolerated across patient groups.",
                    replacement_text="In AURORA-301, Grade >=3 adverse events occurred in 22% of Aurenix patients.",
                )
            ],
        ),
        ReviewComment(
            comment_id="cmt_leg_2",
            reviewer_name="Marcus Vance",
            reviewer_role="Legal",
            timestamp="2026-10-28T09:14:00Z",
            anchored_passage="Aurenix is well tolerated across patient groups.",
            comment_text="Remove safety summary from this paragraph entirely and refer only to Full Prescribing Information.",
            category=CommentCategory.POLICY_QUESTION,
            proposed_spans=[
                WordingSpanEdit(
                    target_text="Aurenix is well tolerated across patient groups.",
                    replacement_text="Please see Full Prescribing Information for complete safety profile.",
                )
            ],
        ),
    ]
    reconcile_res = import_and_reconcile_comments(job=job4, raw_comments=conflicting_comments, router=router)
    results["case_04_conflicting_comments"] = {
        "passed": reconcile_res["requires_human_resolution"] is True
        and len(reconcile_res["conflict_groups"]) == 1
        and all(c.human_decision == "escalated" for c in job4.comments),
        "conflict_groups": reconcile_res["conflict_groups"],
    }

    # Case 5: Amendment that reflows PDF annotations -> re-resolves and validates anchors
    job5 = JobRecord(
        job_id="job_case_05",
        tenant_id="tenant_onco_biotech",
        title="Annotated Launch Release",
        sector=SectorType.PHARMA_PREAPPROVAL,
        document_type="Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T17:00:00Z",
    )
    ingest_working_document(
        job=job5,
        body_text=(
            "In 2L+ EGFR-mutated NSCLC, Aurenix reduced progression-free survival risk by 38% "
            "(HR 0.62; 95% CI, 0.49 to 0.78; p<0.001)."
        ),
        actor="sarah.chen@onco.example",
    )
    locate_and_anchor_evidence(job=job5, source_pack=source_pack, router=router)
    reanchored = reanchor_after_reflow(job=job5, vertical_shift_pt=600.0, page_height_pt=792.0)
    results["case_05_reflowed_annotations"] = {
        "passed": len(reanchored) == 1 and reanchored[0].page_number == 5 and reanchored[0].anchor_valid is True,
        "new_page": reanchored[0].page_number,
        "anchor_valid": reanchored[0].anchor_valid,
    }

    # Case 6: Source update affecting several assets (multi-asset blast radius)
    assets_to_scan = [
        {
            "asset_id": "AST-PR-01",
            "title": "Global Press Release",
            "source_Ids": ["SRC-CSR-301"],
            "body": "Median follow-up was 14.2 months across 410 patients.",
        },
        {
            "asset_id": "AST-QA-02",
            "title": "Investor & Media Q&A",
            "source_Ids": ["SRC-CSR-301"],
            "body": "At the 14.2 months data cutoff, progression-free survival remained consistent.",
        },
        {
            "asset_id": "AST-CORP-03",
            "title": "Employee Town Hall Memo",
            "source_Ids": [],
            "body": "We thank our clinical trial investigators.",
        },
    ]
    blast = compute_multi_asset_blast_radius(
        updated_source_id="SRC-CSR-301",
        old_value="14.2 months",
        new_value="18.6 months",
        assets=assets_to_scan,
    )
    results["case_06_multi_asset_source_update"] = {
        "passed": len(blast) == 2 and all(b["contains_stale_value"] for b in blast),
        "impacted_asset_Ids": [b["asset_id"] for b in blast],
    }

    # Case 7: MedTech device-version mismatch (without inheriting pharmaceutical ISI)
    job7 = JobRecord(
        job_id="job_case_07",
        tenant_id="tenant_medtech_pulse",
        title="CardioPulse Congress Announcement",
        sector=SectorType.MEDTECH,
        document_type="Product Announcement",
        destination_platform="aprimo",
        deadline_iso="2026-11-10T17:00:00Z",
    )
    ingest_working_document(
        job=job7,
        body_text=(
            "The CardioPulse v3.8 ablation catheter is 510(k) cleared for paroxysmal atrial fibrillation "
            "with 94.2% acute isolation."
        ),
        actor="comms@cardiopulse.example",
    )
    claims7 = locate_and_anchor_evidence(job=job7, source_pack=source_pack, router=router)
    results["case_07_medtech_device_version_mismatch"] = {
        "passed": claims7[0].support_status == SupportClassification.CONTRADICTED
        and claims7[0].requires_isi is False
        and "v4.2" in claims7[0].plain_english_diagnosis,
        "diagnosis": claims7[0].plain_english_diagnosis,
        "requires_pharma_isi": claims7[0].requires_isi,
    }

    # Case 8: Approval-day narrowed final indication
    prepared_scenarios = [
        {
            "scenario_id": "SCEN-BROAD-1L",
            "population": "1L and 2L EGFR-mutated NSCLC",
            "boxed_warning": None,
        }
    ]
    final_label_narrowed = {
        "indication": "Aurenix is indicated for adult patients with 2L+ EGFR-mutated NSCLC after platinum chemotherapy.",
        "population": "2L+ EGFR-mutated NSCLC after platinum chemotherapy",
        "boxed_warning": None,
    }
    kit_assets = [
        {
            "asset_id": "KIT-PR-DAY1",
            "title": "Approval Day Press Release",
            "channel": "Press Release",
            "body": "FDA approves Aurenix for 1L and 2L EGFR-mutated NSCLC.",
        }
    ]
    delta8 = analyze_approval_day_label_delta(
        prepared_scenarios=prepared_scenarios,
        final_approved_label=final_label_narrowed,
        launch_kit_assets=kit_assets,
        router=router,
    )
    results["case_08_narrowed_final_indication"] = {
        "passed": delta8["fits_prepared_scenario"] is False
        and delta8["new_review_path_required"] is True
        and delta8["service_tier"] == "priority",
        "out_of_scenario_reasons": delta8["out_of_scenario_reasons"],
    }

    # Case 9: Unanticipated regulatory outcome (new boxed warning / REMS requirement)
    final_label_unexpected = {
        "indication": "Aurenix indicated for 2L+ EGFR-mutated NSCLC.",
        "population": "1L and 2L EGFR-mutated NSCLC",
        "boxed_warning": "WARNING: INTERSTITIAL LUNG DISEASE (ILD) / PNEUMONITIS",
        "unexpected_restriction": "Mandatory REMS pulmonary monitoring program required prior to dispensing.",
    }
    delta9 = analyze_approval_day_label_delta(
        prepared_scenarios=prepared_scenarios,
        final_approved_label=final_label_unexpected,
        launch_kit_assets=kit_assets,
        router=router,
    )
    results["case_09_unanticipated_outcome"] = {
        "passed": delta9["fits_prepared_scenario"] is False
        and any("REMS" in r for r in delta9["out_of_scenario_reasons"]),
        "out_of_scenario_reasons": delta9["out_of_scenario_reasons"],
    }

    # Case 10: Failed / uncertain upload reconciled via idempotency key without duplicate creation
    job10 = JobRecord(
        job_id="job_case_10",
        tenant_id="tenant_onco_biotech",
        title="Veeva Upload Recovery Test",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T17:00:00Z",
    )
    veeva_adapter = CapabilityCheckedPlatformAdapter("veeva_promomats")
    uncertain_rcpt = veeva_adapter.execute_action(
        job=job10,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="sarah.chen@onco.example",
        payload={"file": "release_v1.docx"},
        idempotency_key="idem_job10_v1",
        simulate_uncertain_network_drop=True,
    )
    reconciled_rcpt = veeva_adapter.execute_action(
        job=job10,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="sarah.chen@onco.example",
        payload={"file": "release_v1.docx"},
        idempotency_key="idem_job10_v1",
        simulate_uncertain_network_drop=False,
    )
    results["case_10_failed_upload_idempotent_recovery"] = {
        "passed": uncertain_rcpt.status == "failed_uncertain"
        and reconciled_rcpt.status == "reconciled_no_duplicate"
        and uncertain_rcpt.external_object_id == reconciled_rcpt.external_object_id,
        "external_object_id": reconciled_rcpt.external_object_id,
    }

    # Case 11: Duplicate or delayed platform events handled without state regression
    ev_approve = veeva_adapter.ingest_platform_webhook_event(
        job=job10,
        event_id="evt_veeva_9001",
        event_type="status_changed_approved",
        payload={"approval_record_id": "VEEVA-APPR-2026-771", "approved_version": 1},
    )
    ev_dup = veeva_adapter.ingest_platform_webhook_event(
        job=job10,
        event_id="evt_veeva_9001",
        event_type="status_changed_approved",
        payload={"approval_record_id": "VEEVA-APPR-2026-771", "approved_version": 1},
    )
    ev_delayed_in_review = veeva_adapter.ingest_platform_webhook_event(
        job=job10,
        event_id="evt_veeva_8999_delayed",
        event_type="status_changed_in_review",
        payload={},
    )
    results["case_11_duplicate_or_delayed_events"] = {
        "passed": ev_approve["duplicate_ignored"] is False
        and ev_dup["duplicate_ignored"] is True
        and ev_delayed_in_review["job_state"] == JobLifecycleState.APPROVED.value
        and job10.approved_handoff_bundle_uri is not None,
        "final_state": job10.state.value,
    }

    # Case 12: Attempted cross-client retrieval blocked
    cross_tenant_res = query_statement_reuse_library(
        library=library,
        requesting_tenant_id="tenant_competitor_pharma",
        query_text="Aurenix",
        target_jurisdiction="US",
        target_audience="HCP",
        target_channel="Congress Deck",
        target_product_version="Aurenix-100mg",
    )
    cross_tool_blocked = False
    try:
        genie.invoke_tool(
            "locate_and_anchor_evidence",
            job=job1,
            required_mandate_action="prepare_annotations",
            requesting_tenant_id="tenant_competitor_pharma",
            source_pack=source_pack,
        )
    except PermissionError:
        cross_tool_blocked = True

    results["case_12_cross_client_retrieval_blocked"] = {
        "passed": len(cross_tenant_res) == 0 and cross_tool_blocked is True,
        "returned_records": len(cross_tenant_res),
        "adk_callback_blocked": cross_tool_blocked,
    }

    return results


if __name__ == "__main__":
    import sys

    suite = run_all_12_acceptance_cases()
    all_passed = True
    print("MLR Genie Section 11 Acceptance Evaluation Suite (12 Cases):")
    print("-" * 64)
    for case_name, details in suite.items():
        status = "PASS" if details.get("passed") else "FAIL"
        if not details.get("passed"):
            all_passed = False
        print(f"[{status}] {case_name}")
    print("-" * 64)
    sys.exit(0 if all_passed else 1)

