from __future__ import annotations

from typing import Any

from backend.agent.mlr_genie_agent import MlrGenieAgent
from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.domain.schemas import (
    AssetRevisionSnapshot,
    CommentCategory,
    ConnectorActionType,
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
    apply_wording_span,
    extract_human_resolved_facts,
    ingest_working_document,
    keep_source,
    locate_and_anchor_evidence,
    reanchor_after_reflow,
)
from backend.tools.platform_adapters import CapabilityCheckedPlatformAdapter
from backend.tools.review_tools import (
    analyze_approval_day_label_delta,
    compute_multi_asset_blast_radius,
    import_and_reconcile_comments,
    query_statement_reuse_library,
    screen_pharmacovigilance_cv08,
    validate_strategy,
)


def _build_genie() -> tuple[MlrGenieAgent, GeminiModelRouter]:
    config = GcpAgentConfig()
    router = GeminiModelRouter(config)
    genie = MlrGenieAgent(config=config, router=router)
    genie.register_tool("ingest_working_document", ingest_working_document)
    genie.register_tool("locate_and_anchor_evidence", locate_and_anchor_evidence)
    genie.register_tool("apply_accepted_changes", apply_accepted_changes)
    genie.register_tool("reanchor_after_reflow", reanchor_after_reflow)
    genie.register_tool("screen_pharmacovigilance_cv08", screen_pharmacovigilance_cv08)
    genie.register_tool("validate_strategy", validate_strategy)
    genie.register_tool("import_and_reconcile_comments", import_and_reconcile_comments)
    return genie, router


def run_pharma_comms_scenario_battery() -> dict[str, dict[str, Any]]:
    """
    Executes a 15-scenario real-world Pharma, Biotech & MedTech Communications
    battery covering 7 Majority Workflows (M-01 to M-07) and 8 Edge Cases (E-01 to E-08).
    """
    genie, router = _build_genie()
    results: dict[str, dict[str, Any]] = {}

    # =========================================================================
    # PART I: MAJORITY SCENARIOS (80% Day-to-Day Pharma / Biotech / MedTech Work)
    # =========================================================================

    # M-01: Phase 2 -> Phase 3 R&D-to-Product Comms Handoff ("About the Disease" & Epidemiology)
    job_m01 = JobRecord(
        job_id="scen_m01_rd_to_product_handoff",
        tenant_id="tenant_celvanta",
        title="Celvanta (BTX-409) Phase 3 Initiation Release & 'About HS' Boilerplate",
        sector=SectorType.BIOTECH,
        document_type="Phase 3 Initiation Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-05T13:00:00Z",
    )
    rd_phase2_source_pack = [
        {
            "source_id": "rd_ib_v4_ph2",
            "version": "IB-v4.0",
            "verbatim_text": (
                "Hidradenitis suppurativa (HS) is a chronic inflammatory follicular skin disease "
                "with a global prevalence of 1% to 4% and affects approximately 3.4 million U.S. adults."
            ),
            "keywords": ["hidradenitis suppurativa", "prevalence", "adults", "americans"],
            "locked_numbers": ["1%", "4%", "3.4"],
            "required_qualifiers": ["1% to 4%", "3.4 million U.S. adults"],
            "bbox": {"page": 8, "x0": 82.0, "y0": 110.0, "x1": 515.0, "y1": 195.0},
        }
    ]
    m01_draft = (
        "Hidradenitis suppurativa (HS) is a chronic inflammatory follicular skin disease with a global prevalence of 1% to 4% and affects approximately 3.4 million U.S. adults. "
        "Hidradenitis suppurativa currently affects 12 million Americans without any treatment options."
    )
    genie.invoke_tool(
        "ingest_working_document",
        job=job_m01,
        body_text=m01_draft,
        actor="human:product.comms@celvanta.com",
    )
    genie.invoke_tool(
        "locate_and_anchor_evidence",
        job=job_m01,
        required_mandate_action="prepare_annotations",
        source_pack=rd_phase2_source_pack,
        router=router,
    )
    results["M01_phase2_to_phase3_rd_handoff_boilerplate"] = {
        "category": "majority",
        "scenario": "Phase 2 -> Phase 3 R&D-to-Product Comms Handoff ('About the Disease' Boilerplate & Epidemiology)",
        "passed": (
            len(job_m01.claims) == 2
            and job_m01.claims[0].support_status == SupportClassification.SUPPORTED
            and job_m01.claims[0].anchors[0].source_document_id == "rd_ib_v4_ph2"
            and job_m01.claims[1].support_status == SupportClassification.CONTRADICTED
        ),
        "carried_over_rd_anchor": job_m01.claims[0].anchors[0].source_document_id,
        "drifted_epidemiology_diagnosis": job_m01.claims[1].plain_english_diagnosis,
    }

    # M-02: Phase 3 Topline Readout Morning (8:15 AM Press Release + Dropped 95% CI & Comparator Qualifier)
    job_m02 = JobRecord(
        job_id="scen_m02_topline_morning",
        tenant_id="tenant_aurenix",
        title="8:15 AM Phase 3 Topline Readout Press Release",
        sector=SectorType.PHARMA_PREAPPROVAL,
        document_type="Topline Readout Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-28T08:30:00Z",
    )
    m02_tlf_pack = [
        {
            "source_id": "tlf_aur301_lock",
            "version": "TLF-v1.0",
            "verbatim_text": (
                "Aurenix 100mg reduced primary flaring events by 34% vs placebo at Week 16 (p=0.0012; 95% CI: 0.54-0.82)."
            ),
            "keywords": ["aurenix", "flaring", "34%"],
            "locked_numbers": ["34%", "0.0012"],
            "required_qualifiers": ["vs placebo", "Week 16", "95% CI: 0.54-0.82"],
            "bbox": {"page": 4, "x0": 75.0, "y0": 140.0, "x1": 530.0, "y1": 230.0},
        }
    ]
    m02_agency_draft = "Aurenix 100mg reduced primary flaring events by 34% (p=0.0012)."
    genie.invoke_tool(
        "ingest_working_document",
        job=job_m02,
        body_text=m02_agency_draft,
        actor="human:vp.comms@aurenix.com",
    )
    genie.invoke_tool(
        "locate_and_anchor_evidence",
        job=job_m02,
        required_mandate_action="prepare_annotations",
        source_pack=m02_tlf_pack,
        router=router,
    )
    results["M02_phase3_topline_readout_missing_ci_qualifier"] = {
        "category": "majority",
        "scenario": "Phase 3 Topline Morning — Agency Draft Drops 95% CI & Comparator Qualifier",
        "passed": (
            len(job_m02.claims) == 1
            and job_m02.claims[0].support_status == SupportClassification.PARTIALLY_SUPPORTED
            and "95% CI: 0.54-0.82" in job_m02.claims[0].missing_qualifiers
            and "vs placebo" in job_m02.claims[0].missing_qualifiers
        ),
        "missing_qualifiers_detected": job_m02.claims[0].missing_qualifiers,
        "plain_english_diagnosis": job_m02.claims[0].plain_english_diagnosis,
    }

    # M-03: Multi-Channel Agency Adaptation (Press Release -> LinkedIn Post dropping population & ISI)
    job_m03 = JobRecord(
        job_id="scen_m03_social_adaptation",
        tenant_id="tenant_oncovex",
        title="OncoVex ASCO LinkedIn Adaptation (<=160 chars)",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Social Media Post (LinkedIn)",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-30T12:00:00Z",
    )
    m03_pi_pack = [
        {
            "source_id": "oncovex_uspi",
            "version": "USPI-2026",
            "verbatim_text": (
                "In adult patients with 2L+ EGFR-mutated metastatic NSCLC, OncoVex reduced the risk of progression or death by 38% vs platinum chemotherapy (HR 0.62; 95% CI: 0.49-0.78)."
            ),
            "keywords": ["oncovex", "progression", "38%"],
            "locked_numbers": ["38%"],
            "required_qualifiers": ["2L+ EGFR-mutated metastatic NSCLC", "vs platinum chemotherapy"],
            "bbox": {"page": 1, "x0": 80.0, "y0": 100.0, "x1": 510.0, "y1": 190.0},
        }
    ]
    m03_linkedin_draft = "OncoVex reduced the risk of progression or death by 38% in lung cancer patients."
    genie.invoke_tool(
        "ingest_working_document",
        job=job_m03,
        body_text=m03_linkedin_draft,
        actor="human:agency.writer@pr.com",
    )
    genie.invoke_tool(
        "locate_and_anchor_evidence",
        job=job_m03,
        required_mandate_action="prepare_annotations",
        source_pack=m03_pi_pack,
        router=router,
    )
    results["M03_multichannel_social_adaptation_qualifier_and_isi_check"] = {
        "category": "majority",
        "scenario": "Multi-Channel Agency Check — LinkedIn Post Drops Biomarker Population & Paired ISI",
        "passed": (
            len(job_m03.claims) == 1
            and "2L+ EGFR-mutated metastatic NSCLC" in job_m03.claims[0].missing_qualifiers
            and job_m03.claims[0].paired_isi_present is False
        ),
        "missing_qualifiers": job_m03.claims[0].missing_qualifiers,
        "paired_isi_present": job_m03.claims[0].paired_isi_present,
    }

    # M-04: FDA Approval-Day Launch Pack Blitz (Draft Label -> Final USPI Narrowed Indication)
    m04_delta = analyze_approval_day_label_delta(
        prepared_scenarios=[
            {
                "scenario_id": "scen_broad_12plus",
                "population": "adult and adolescent patients aged 12 years and older",
                "boxed_warning": None,
            }
        ],
        final_approved_label={
            "indication": "Treatment of adult patients (18 years and older) with moderate-to-severe plaque psoriasis",
            "population": "adult patients (18 years and older)",
            "boxed_warning": None,
        },
        launch_kit_assets=[
            {
                "asset_id": "asset_pr_01",
                "title": "Aurenix Launch Press Release",
                "channel": "Press Release",
                "body": "Aurenix is now approved for adult and adolescent patients aged 12 years and older.",
            },
            {
                "asset_id": "asset_hcp_02",
                "title": "Aurenix HCP Core Visual Aid",
                "channel": "HCP Deck",
                "body": "Prescribe Aurenix for patients aged 12+ with plaque psoriasis.",
            },
        ],
        router=router,
    )
    results["M04_approval_day_final_uspi_delta_priority_inference"] = {
        "category": "majority",
        "scenario": "PDUFA Approval-Day Final USPI Delta (Priority Inference on gemini-4-argon)",
        "passed": (
            m04_delta["model_used"] == "gemini-4-argon"
            and m04_delta["service_tier"] == "priority"
            and m04_delta["fits_prepared_scenario"] is False
            and len(m04_delta["affected_assets"]) == 2
        ),
        "model_used": m04_delta["model_used"],
        "service_tier": m04_delta["service_tier"],
        "affected_assets_count": len(m04_delta["affected_assets"]),
    }

    # M-05: MedTech 510(k) Clearance & IFU Software Version Mismatch
    job_m05 = JobRecord(
        job_id="scen_m05_medtech_ifu",
        tenant_id="tenant_pulsenav",
        title="PulseNav 3D Electrophysiology VAC Deck",
        sector=SectorType.MEDTECH,
        document_type="Hospital Value Analysis Committee Deck",
        destination_platform="aprimo",
        deadline_iso="2026-11-02T17:00:00Z",
    )
    m05_ifu_pack = [
        {
            "source_id": "ifu_k261044",
            "version": "IFU-v3.2",
            "verbatim_text": "PulseNav 3D Software v3.2 activates acoustic contact force alerts at 40g.",
            "keywords": ["pulsenav", "software", "alerts"],
            "locked_numbers": ["40"],
            "device_version": "v3.2",
            "bbox": {"page": 11, "x0": 95.0, "y0": 180.0, "x1": 495.0, "y1": 260.0},
        }
    ]
    genie.invoke_tool(
        "ingest_working_document",
        job=job_m05,
        body_text="PulseNav 3D Software v3.1 activates acoustic contact force alerts at 40g.",
        actor="human:medtech.marketing@pulsenav.com",
    )
    genie.invoke_tool(
        "locate_and_anchor_evidence",
        job=job_m05,
        required_mandate_action="prepare_annotations",
        source_pack=m05_ifu_pack,
        router=router,
    )
    results["M05_medtech_510k_ifu_device_version_mismatch"] = {
        "category": "majority",
        "scenario": "MedTech 510(k) Brochure Cites Software v3.1 Against Cleared IFU v3.2",
        "passed": (
            len(job_m05.claims) == 1
            and job_m05.claims[0].support_status == SupportClassification.CONTRADICTED
            and "v3.1" in job_m05.claims[0].plain_english_diagnosis
            and "v3.2" in job_m05.claims[0].plain_english_diagnosis
        ),
        "diagnosis": job_m05.claims[0].plain_english_diagnosis,
    }

    # M-06: Medical Congress Scientific Exchange vs. Pre-Approval Promotional Superlative Check
    job_m06 = JobRecord(
        job_id="scen_m06_congress_exchange",
        tenant_id="tenant_celvanta",
        title="EADV Congress Scientific Symposium Abstract & Social Recap",
        sector=SectorType.BIOTECH,
        document_type="Congress Symposium Recap",
        destination_platform="veeva_promomats",
        deadline_iso="2026-11-10T09:00:00Z",
    )
    m06_findings = genie.invoke_tool(
        "validate_strategy",
        job=job_m06,
        text_to_check="BTX-409 is a best-in-class therapy with guaranteed clinical remission.",
    )
    results["M06_congress_scientific_exchange_vs_promotional_superlatives"] = {
        "category": "majority",
        "scenario": "Congress Scientific Exchange — Pre-Approval Promotional Superlatives Flagged as Advisory Note",
        "passed": (
            len(m06_findings) == 2
            and all(f.severity == "note" for f in m06_findings)
        ),
        "advisory_codes": [f.code for f in m06_findings],
    }

    # M-07: Multi-Round MLR Reviewer Comment Reconciliation (Clean Atomic Edits + Medical/Regulatory Conflict)
    job_m07 = JobRecord(
        job_id="scen_m07_reviewer_reconciliation",
        tenant_id="tenant_aurenix",
        title="Aurenix Core Visual Aid Round 2 MLR Reconciliation",
        sector=SectorType.PHARMA_APPROVED,
        document_type="HCP Core Visual Aid",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-31T17:00:00Z",
    )
    m07_comments = [
        ReviewComment(
            comment_id="cmt_med_1",
            reviewer_role="Medical",
            reviewer_name="Dr. E. Vance",
            timestamp="2026-10-28T14:00:00Z",
            anchored_passage="Slide 5: Aurenix demonstrated efficacy.",
            comment_text="Include N=412 subgroup response rate on Slide 5.",
            category=CommentCategory.MISSING_SUPPORT,
            proposed_spans=[
                WordingSpanEdit(
                    target_text="demonstrated efficacy.",
                    replacement_text="demonstrated efficacy in the N=412 subgroup.",
                )
            ],
        ),
        ReviewComment(
            comment_id="cmt_reg_2",
            reviewer_role="Regulatory",
            reviewer_name="M. Chen",
            timestamp="2026-10-28T14:05:00Z",
            anchored_passage="Slide 5: Aurenix demonstrated efficacy.",
            comment_text="Delete all subgroup data on Slide 5; not powered for multiplicity.",
            category=CommentCategory.FACTUAL_CORRECTION,
            proposed_spans=[
                WordingSpanEdit(
                    target_text="demonstrated efficacy.",
                    replacement_text="was evaluated in the primary ITT population.",
                )
            ],
        ),
    ]
    m07_reconciled = genie.invoke_tool(
        "import_and_reconcile_comments",
        job=job_m07,
        raw_comments=m07_comments,
        router=router,
    )
    results["M07_multi_reviewer_conflict_consolidation"] = {
        "category": "majority",
        "scenario": "Medical vs. Regulatory Reviewer Conflict Consolidated Into Single Human Decision Card",
        "passed": (
            m07_reconciled["requires_human_resolution"] is True
            and len(m07_reconciled["conflict_groups"]) == 1
        ),
        "conflicts_detected": len(m07_reconciled["conflict_groups"]),
    }

    # =========================================================================
    # PART II: TREACHEROUS REAL-WORLD EDGE CASES (E-01 to E-08)
    # =========================================================================

    # E-01: Complete Response Letter (CRL) / Unanticipated Regulatory Outcome on PDUFA Day
    e01_crl = analyze_approval_day_label_delta(
        prepared_scenarios=[
            {
                "scenario_id": "scen_standard_approval",
                "population": "adult patients",
                "boxed_warning": None,
            }
        ],
        final_approved_label={
            "indication": "",
            "population": "",
            "boxed_warning": None,
            "unexpected_restriction": "Complete Response Letter (CRL): third-party fill-finish CMC inspection observation; promotional launch pack frozen.",
        },
        launch_kit_assets=[
            {
                "asset_id": "asset_launch_pr",
                "title": "Day-1 Commercial Launch Press Release",
                "channel": "Press Release",
                "body": "Aurenix is now commercially available nationwide.",
            }
        ],
        router=router,
    )
    results["E01_pdufa_day_complete_response_letter_freeze"] = {
        "category": "edge_case",
        "scenario": "Unanticipated CRL on PDUFA Day — Out-of-Scenario Detection Freezes Launch Assets",
        "passed": (
            e01_crl["fits_prepared_scenario"] is False
            and e01_crl["new_review_path_required"] is True
            and len(e01_crl["affected_assets"]) == 1
        ),
        "out_of_scenario_reasons": e01_crl["out_of_scenario_reasons"],
    }

    # E-02: 24-Hour Pharmacovigilance / Adverse Event (CV-08) Intercept in Free-Text Comms Intake
    job_e02 = JobRecord(
        job_id="scen_e02_pv_intercept",
        tenant_id="tenant_aurenix",
        title="Community Manager Social Reply Draft",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Social Media Reply",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-28T19:00:00Z",
    )
    e02_pv = genie.invoke_tool(
        "screen_pharmacovigilance_cv08",
        job=job_e02,
        intake_text="Community inbox: A patient was hospitalized after taking Aurenix 100mg last Tuesday with acute jaundice.",
        router=router,
    )
    results["E02_pharmacovigilance_cv08_24hr_adverse_event_intercept"] = {
        "category": "edge_case",
        "scenario": "24-Hour Pharmacovigilance (CV-08) Gate Intercepts Patient Adverse Event in Comms Intake",
        "passed": (
            e02_pv["cv08_flagged"] is True
            and job_e02.pv_icrs_flagged is True
        ),
        "cv08_flagged": e02_pv["cv08_flagged"],
        "routing_note": e02_pv["routing_note"],
    }

    # E-03: Cross-Indication / Cross-Jurisdiction Statement Reuse Trap (US HCP vs EU Public)
    reuse_library = [
        StatementReuseRecord(
            statement_id="stmt_us_hcp_1",
            tenant_id="tenant_aurenix",
            exact_wording="In AUR-301, Aurenix 100mg achieved a 34% reduction in primary flaring events at Week 16 (p=0.0012).",
            source_document_id="doc_csr_301",
            source_version="CSR-v3",
            source_location="Page 42, Table 14.2.1",
            originating_material_id="MAT-US-2026-01",
            reviewer_role="Medical & Regulatory",
            reviewer_rationale="Cleared for US HCP Congress presentation only.",
            product_or_device_version="Aurenix-100mg",
            sector=SectorType.PHARMA_APPROVED,
            jurisdiction="US",
            audience="HCP",
            channel="Congress Deck",
            purpose="Scientific Congress Presentation",
            valid_from="2026-01-01",
            valid_until="2027-12-31",
            evidence_support=ReuseDimensionEvidence.VERIFIED_SOURCE,
            public_disclosure=ReuseDimensionDisclosure.PUBLIC_COMPANY_FOOTPRINT,
            review_decision=ReuseDimensionDecision.CLEARED_IN_CONTEXT,
            permitted_contexts=["US:HCP:Congress Deck"],
        )
    ]
    eu_dtc_eval = query_statement_reuse_library(
        library=reuse_library,
        requesting_tenant_id="tenant_aurenix",
        query_text="Aurenix 100mg achieved a 34% reduction",
        target_jurisdiction="EU",
        target_audience="General Public",
        target_channel="DTC Website",
        target_product_version="Aurenix-100mg",
    )
    results["E03_cross_jurisdiction_us_hcp_to_eu_public_reuse_blocked"] = {
        "category": "edge_case",
        "scenario": "Cross-Jurisdiction / Cross-Audience Trap — Blocks Direct US HCP Claim Reuse on EU Public Webpage",
        "passed": (
            len(eu_dtc_eval) == 1
            and eu_dtc_eval[0]["can_reuse_directly"] is False
            and len(eu_dtc_eval[0]["reasons_requiring_rereview"]) >= 2
        ),
        "can_reuse_directly": eu_dtc_eval[0]["can_reuse_directly"],
        "reasons_requiring_rereview": eu_dtc_eval[0]["reasons_requiring_rereview"],
    }

    # E-04: Cross-Tenant Isolation & Unauthorized Mandate Gate (ADK before_tool_callback)
    cross_tenant_blocked = False
    try:
        genie.invoke_tool(
            "locate_and_anchor_evidence",
            job=job_m01,
            required_mandate_action="prepare_annotations",
            requesting_tenant_id="tenant_competitor_intruder",
            source_pack=rd_phase2_source_pack,
            router=router,
        )
    except PermissionError:
        cross_tenant_blocked = True

    results["E04_adk_callback_cross_tenant_and_mandate_isolation"] = {
        "category": "edge_case",
        "scenario": "ADK before_tool_callback Blocks Cross-Tenant Intrusion & Unauthorized Tool Calls",
        "passed": cross_tenant_blocked is True,
        "cross_tenant_blocked": cross_tenant_blocked,
    }

    # E-05: Document Reflow & Partial Multi-Span Failure (Zero Silent Mutation Atomic Rollback)
    job_e05 = JobRecord(
        job_id="scen_e05_reflow_rollback",
        tenant_id="tenant_aurenix",
        title="Aurenix Reflowed Press Release",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Press Release",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-30T17:00:00Z",
    )
    reflowed_body = (
        "Aurenix 100mg is indicated for adult patients with moderate-to-severe plaque psoriasis. "
        "Paragraph two was rewritten by the brand team this morning."
    )
    ingest_working_document(
        job=job_e05,
        body_text=reflowed_body,
        actor="human:brand.writer@aurenix.com",
    )
    multi_span_comment = ReviewComment(
        comment_id="cmt_reflow_atomic",
        reviewer_role="Legal",
        reviewer_name="S. Patel",
        timestamp="2026-10-28T15:00:00Z",
        anchored_passage="Paragraph 1 & 2",
        comment_text="Two-part edit across Paragraphs 1 and 2.",
        category=CommentCategory.FACTUAL_CORRECTION,
        human_decision="accepted",
        proposed_spans=[
            WordingSpanEdit(
                target_text="Aurenix 100mg",
                replacement_text="AURENIX (100 mg)",
            ),
            WordingSpanEdit(
                target_text="Original paragraph two sentence before brand team rewrite.",
                replacement_text="Updated paragraph two sentence.",
            ),
        ],
    )
    apply_res = apply_accepted_changes(
        job=job_e05,
        comments=[multi_span_comment],
        human_actor="human:mlr.coord@aurenix.com",
    )
    kept_body = keep_source(
        job=job_e05,
        comment=multi_span_comment,
        human_actor="human:mlr.coord@aurenix.com",
    )
    punct_ok_body, punct_ok = apply_wording_span(
        reflowed_body,
        WordingSpanEdit(
            target_text="plaque psoriasis.",
            replacement_text="plaque psoriasis (see Section 1.1).",
        ),
    )
    results["E05_document_reflow_atomic_rollback_and_punctuation_integrity"] = {
        "category": "edge_case",
        "scenario": "Zero Silent Mutation — Multi-Span Partial Failure Rolls Back Atomically to 'unplaced'",
        "passed": (
            "cmt_reflow_atomic" in apply_res["unplaced_comment_Ids"]
            and apply_res["updated_body"] == reflowed_body
            and multi_span_comment.placement_status == "kept_source"
            and kept_body == reflowed_body
            and punct_ok is True
            and "plaque psoriasis (see Section 1.1)." in punct_ok_body
        ),
        "unplaced_comment_Ids": apply_res["unplaced_comment_Ids"],
        "body_preserved_verbatim": apply_res["updated_body"] == reflowed_body,
    }

    # E-06: Multi-Asset Source Update Blast Radius (Updated Clinical Reference Across Campaign Pack)
    e06_blast = compute_multi_asset_blast_radius(
        updated_source_id="doc_csr_301",
        old_value="31.5%",
        new_value="34.0%",
        assets=[
            {
                "asset_id": "asset_pr",
                "title": "Launch Press Release",
                "source_Ids": ["doc_csr_301"],
                "body": "In AUR-301, Aurenix achieved a 31.5% reduction in primary events.",
            },
            {
                "asset_id": "asset_social",
                "title": "Congress LinkedIn Card",
                "source_Ids": ["doc_csr_301"],
                "body": "AUR-301 data: 31.5% reduction at Week 16.",
            },
            {
                "asset_id": "asset_unlinked_corp",
                "title": "Corporate Fact Sheet",
                "source_Ids": ["doc_corp_01"],
                "body": "Founded in 2018, Aurenix Biopharma is headquartered in Boston.",
            },
        ],
    )
    results["E06_multi_asset_source_update_blast_radius"] = {
        "category": "edge_case",
        "scenario": "Updated Clinical Table Flags All Dependent Assets Still Citing Stale Interim Number",
        "passed": (
            len(e06_blast) == 2
            and all(item["contains_stale_value"] is True for item in e06_blast)
        ),
        "impacted_asset_Ids": [item["asset_id"] for item in e06_blast],
    }

    # E-07: Human vs. Model Provenance Separation (Invariant 1: AssetEditRecorded vs AssetDrafted)
    baseline_body = "Aurenix 100mg is indicated for adult patients."
    revisions = [
        AssetRevisionSnapshot(
            revision_id="rev_ai_1",
            version_number=1,
            body="Aurenix 100mg is indicated for adult patients. AI drafted bridge sentence.",
            sha256_hash="h1",
            event_type=ProvenanceEventType.ASSET_DRAFTED,
            actor="agent:gemini-3.8-flash",
        ),
        AssetRevisionSnapshot(
            revision_id="rev_human_2",
            version_number=2,
            body="Aurenix 100mg is indicated for adult patients. Monitor liver enzymes every 8 weeks.",
            sha256_hash="h2",
            event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
            actor="human:vp.comms@aurenix.com",
        ),
    ]
    human_facts = extract_human_resolved_facts(
        initial_payload_body=baseline_body,
        revisions=revisions,
    )
    results["E07_strict_human_vs_model_provenance_invariant_1"] = {
        "category": "edge_case",
        "scenario": "Invariant 1 — Model Redrafts (AssetDrafted) Never Classified as Customer-Supplied Fact",
        "passed": (
            len(human_facts) == 1
            and human_facts[0] == "Monitor liver enzymes every 8 weeks."
        ),
        "extracted_human_facts": human_facts,
    }

    # E-08: Mid-Upload Timeout Idempotent Recovery & Aprimo DAM-Only vs. Productivity Separation
    job_e08 = JobRecord(
        job_id="scen_e08_connector_resilience",
        tenant_id="tenant_aurenix",
        title="Aurenix Submission Bundle Upload",
        sector=SectorType.PHARMA_APPROVED,
        document_type="Veeva & Aprimo Submission",
        destination_platform="veeva_promomats",
        deadline_iso="2026-10-28T20:00:00Z",
    )
    veeva_adapter = CapabilityCheckedPlatformAdapter("veeva_promomats")
    aprimo_dam_adapter = CapabilityCheckedPlatformAdapter("aprimo_dam")

    rcpt_first = veeva_adapter.execute_action(
        job=job_e08,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="human:mlr.coord@aurenix.com",
        payload={"version": 1, "title": job_e08.title},
        idempotency_key="idem_e08_veeva_v1",
        simulate_uncertain_network_drop=True,
    )
    rcpt_retry = veeva_adapter.execute_action(
        job=job_e08,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="human:mlr.coord@aurenix.com",
        payload={"version": 1, "title": job_e08.title},
        idempotency_key="idem_e08_veeva_v1",
    )
    aprimo_dam_comment_rcpt = aprimo_dam_adapter.execute_action(
        job=job_e08,
        action=ConnectorActionType.READ_COMMENTS_STATUS,
        actor_human="human:mlr.coord@aurenix.com",
        payload={},
        idempotency_key="idem_e08_aprimo_dam_comments",
    )
    results["E08_connector_timeout_idempotency_and_aprimo_tier_separation"] = {
        "category": "edge_case",
        "scenario": "Veeva Mid-Upload Timeout Reconciles Without Duplicates & Aprimo DAM-Only Degrades Gracefully",
        "passed": (
            rcpt_first.external_object_id == rcpt_retry.external_object_id
            and rcpt_retry.read_back_verified is True
            and aprimo_dam_comment_rcpt.status == "unsupported_limitation"
        ),
        "veeva_external_object_id": rcpt_retry.external_object_id,
        "aprimo_dam_comment_status": aprimo_dam_comment_rcpt.status,
    }

    return results


if __name__ == "__main__":
    import sys

    battery = run_pharma_comms_scenario_battery()
    all_ok = True
    print("MLR Genie Real-World Pharma, Biotech & MedTech Scenario Battery (15 Scenarios):")
    print("=" * 94)
    for key, item in battery.items():
        status = "PASS" if item["passed"] else "FAIL"
        if not item["passed"]:
            all_ok = False
        cat = item["category"].upper().ljust(9)
        print(f"[{status}] [{cat}] {key}: {item['scenario']}")
    print("=" * 94)
    sys.exit(0 if all_ok else 1)
