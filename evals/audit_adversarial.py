from __future__ import annotations

import importlib
from typing import Any
from fastapi.testclient import TestClient

import backend.main as main_mod
from backend.agent.models_router import GeminiModelRouter, ModelCallTelemetry
from backend.domain.schemas import (
    AssetRevisionSnapshot,
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
    extract_human_resolved_facts,
    ingest_working_document,
    locate_and_anchor_evidence,
)
from backend.tools.platform_adapters import CapabilityCheckedPlatformAdapter
from backend.tools.review_tools import (
    analyze_approval_day_label_delta,
    import_and_reconcile_comments,
    query_statement_reuse_library,
    screen_pharmacovigilance_cv08,
)


def _make_job(
    job_id: str,
    tenant_id: str = "tenant_a",
    mandate: list[str] | None = None,
) -> JobRecord:
    kwargs: dict[str, Any] = {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "title": f"Adversarial Audit Job {job_id}",
        "sector": SectorType.PHARMA_PREAPPROVAL,
        "document_type": "Press Release",
        "destination_platform": "veeva_promomats",
        "deadline_iso": "2026-11-15T17:00:00Z",
    }
    if mandate is not None:
        kwargs["mandate_allowed_actions"] = mandate
    return JobRecord(**kwargs)


def run_all_27_adversarial_probes() -> dict[str, dict[str, Any]]:
    """
    Executes all 27 adversarial probes from the Independent Audit (28 October 2026).
    Every probe tests the actual underlying mechanism rather than surface labels.
    """
    results: dict[str, dict[str, Any]] = {}

    # 1. Semantic negation: Source says "did not improve overall survival"; positive claim must be CONTRADICTED
    job_p1 = _make_job("probe_01")
    ingest_working_document(
        job=job_p1,
        body_text="In the Phase 3 study, Aurenix improved overall survival across all patients.",
        actor="tester@tenant_a.com",
    )
    claims_p1 = locate_and_anchor_evidence(
        job=job_p1,
        source_pack=[
            {
                "source_id": "SRC-NEG",
                "version": "1.0",
                "verbatim_text": "In the Phase 3 study, Aurenix did not improve overall survival compared with control.",
                "keywords": ["aurenix", "overall survival"],
            }
        ],
    )
    results["probe_01_semantic_negation"] = {
        "probe_number": 1,
        "passed": claims_p1[0].support_status == SupportClassification.CONTRADICTED,
        "observed": claims_p1[0].support_status.value,
        "diagnosis": claims_p1[0].plain_english_diagnosis,
    }

    # 2. Empty source: Empty verbatim_text must never produce supported or valid anchor
    job_p2 = _make_job("probe_02")
    ingest_working_document(
        job=job_p2,
        body_text="Aurenix demonstrated durable clinical responses.",
        actor="tester@tenant_a.com",
    )
    claims_p2 = locate_and_anchor_evidence(
        job=job_p2,
        source_pack=[
            {
                "source_id": "SRC-EMPTY",
                "version": "1.0",
                "verbatim_text": "   ",
                "keywords": ["aurenix", "durable"],
            }
        ],
    )
    results["probe_02_empty_source"] = {
        "probe_number": 2,
        "passed": claims_p2[0].support_status == SupportClassification.UNRESOLVED
        and len(claims_p2[0].anchors) == 0,
        "observed_status": claims_p2[0].support_status.value,
        "anchor_count": len(claims_p2[0].anchors),
    }

    # 3. Numerical substring: Source locks 34%, copy says 134% -> must be CONTRADICTED
    job_p3 = _make_job("probe_03")
    ingest_working_document(
        job=job_p3,
        body_text="Aurenix achieved a 134% reduction in disease progression.",
        actor="tester@tenant_a.com",
    )
    claims_p3 = locate_and_anchor_evidence(
        job=job_p3,
        source_pack=[
            {
                "source_id": "SRC-NUM",
                "version": "1.0",
                "verbatim_text": "Aurenix achieved a 34% reduction in disease progression at Week 16.",
                "keywords": ["aurenix", "reduction"],
                "locked_numbers": ["34%"],
            }
        ],
    )
    results["probe_03_numerical_substring"] = {
        "probe_number": 3,
        "passed": claims_p3[0].support_status == SupportClassification.CONTRADICTED,
        "observed": claims_p3[0].support_status.value,
    }

    # 4. Legitimate omission: Copy omits 34% but includes Week 16 -> must NOT be misclassified as contradicted
    job_p4 = _make_job("probe_04")
    ingest_working_document(
        job=job_p4,
        body_text="Aurenix was evaluated for disease progression at Week 16.",
        actor="tester@tenant_a.com",
    )
    claims_p4 = locate_and_anchor_evidence(
        job=job_p4,
        source_pack=[
            {
                "source_id": "SRC-OMIT",
                "version": "1.0",
                "verbatim_text": "Aurenix achieved a 34% reduction in disease progression at Week 16.",
                "keywords": ["aurenix", "week 16"],
                "locked_numbers": ["34%"],
            }
        ],
    )
    results["probe_04_legitimate_omission"] = {
        "probe_number": 4,
        "passed": claims_p4[0].support_status != SupportClassification.CONTRADICTED,
        "observed": claims_p4[0].support_status.value,
    }

    # 5. Invalid PDF coordinates: Negative/out-of-page coordinates must set anchor_valid=False and downgrade support
    job_p5 = _make_job("probe_05")
    ingest_working_document(
        job=job_p5,
        body_text="Aurenix reduced flaring events by 34% at Week 16.",
        actor="tester@tenant_a.com",
    )
    claims_p5 = locate_and_anchor_evidence(
        job=job_p5,
        source_pack=[
            {
                "source_id": "SRC-BAD-BBOX",
                "version": "1.0",
                "verbatim_text": "Aurenix reduced flaring events by 34% at Week 16.",
                "keywords": ["aurenix", "34%"],
                "locked_numbers": ["34%"],
                "bbox": {"page": 1, "x0": -25.0, "y0": 140.0, "x1": 900.0, "y1": 950.0},
            }
        ],
    )
    results["probe_05_invalid_pdf_coordinates"] = {
        "probe_number": 5,
        "passed": (
            claims_p5[0].anchors[0].anchor_valid is False
            and claims_p5[0].support_status == SupportClassification.UNRESOLVED
        ),
        "anchor_valid": claims_p5[0].anchors[0].anchor_valid,
        "support_status": claims_p5[0].support_status.value,
    }

    # Prepare clean HTTP TestClient for Probes 6, 7, 8, 9, 10
    main_mod.JOBS_DB.clear()
    client = TestClient(main_mod.app)

    # 6. Unauthenticated intake: POST /jobs/intake with no auth headers must be rejected (401/403)
    unauth_resp = client.post(
        "/jobs/intake",
        json={
            "job": _make_job("probe_06_job").model_dump(),
            "initial_body": "Aurenix clinical update.",
            "actor": "tester@tenant_a.com",
            "source_pack": [],
        },
    )
    results["probe_06_unauthenticated_intake"] = {
        "probe_number": 6,
        "passed": unauth_resp.status_code in (401, 403),
        "status_code": unauth_resp.status_code,
    }

    # 7. Cross-tenant comments access: Tenant-B header against Tenant-A job must return HTTP 403
    intake_a = client.post(
        "/jobs/intake",
        headers={"X-Tenant-Id": "tenant_a", "X-Actor-Id": "alice@tenant_a.com"},
        json={
            "job": _make_job("probe_07_job", tenant_id="tenant_a").model_dump(),
            "initial_body": "Aurenix Phase 3 data summary.",
            "actor": "alice@tenant_a.com",
            "source_pack": [],
        },
    )
    cross_comments_resp = client.post(
        "/jobs/probe_07_job/comments",
        headers={"X-Tenant-Id": "tenant_b", "X-Actor-Id": "mallory@tenant_b.com"},
        json=[],
    )
    results["probe_07_cross_tenant_comments_access"] = {
        "probe_number": 7,
        "passed": intake_a.status_code == 200 and cross_comments_resp.status_code == 403,
        "status_code": cross_comments_resp.status_code,
    }

    # 8. Job-ID collision: Second intake under tenant_b with same job_id must not replace tenant_a's job
    collision_resp = client.post(
        "/jobs/intake",
        headers={"X-Tenant-Id": "tenant_b", "X-Actor-Id": "mallory@tenant_b.com"},
        json={
            "job": _make_job("probe_07_job", tenant_id="tenant_b").model_dump(),
            "initial_body": "Overwritten by tenant_b",
            "actor": "mallory@tenant_b.com",
            "source_pack": [],
        },
    )
    stored_after_collision = main_mod.JOBS_DB.get("probe_07_job")
    results["probe_08_job_id_collision"] = {
        "probe_number": 8,
        "passed": (
            collision_resp.status_code in (403, 409)
            and stored_after_collision is not None
            and stored_after_collision.tenant_id == "tenant_a"
        ),
        "status_code": collision_resp.status_code,
        "preserved_owner": stored_after_collision.tenant_id if stored_after_collision else None,
    }

    # 9. PV intake stop: Flagged job must halt immediately with state=hold, 0 revisions, and 0 claims
    pv_intake_resp = client.post(
        "/jobs/intake",
        headers={"X-Tenant-Id": "tenant_a", "X-Actor-Id": "alice@tenant_a.com"},
        json={
            "job": _make_job("probe_09_pv_job", tenant_id="tenant_a").model_dump(),
            "initial_body": "A 64-year-old patient experienced severe nausea and was hospitalized after taking Aurenix.",
            "actor": "alice@tenant_a.com",
            "source_pack": [],
        },
    )
    pv_job_data = pv_intake_resp.json()["job"]
    results["probe_09_pv_intake_stop"] = {
        "probe_number": 9,
        "passed": (
            pv_intake_resp.status_code == 200
            and pv_job_data["pv_icrs_flagged"] is True
            and pv_job_data["state"] == JobLifecycleState.HOLD.value
            and len(pv_job_data["revisions"]) == 0
            and len(pv_job_data["claims"]) == 0
        ),
        "state": pv_job_data["state"],
        "revisions_count": len(pv_job_data["revisions"]),
        "claims_count": len(pv_job_data["claims"]),
    }

    # 10. PV submit hold: Flagged job cannot be submitted via HTTP or adapter
    pv_submit_resp = client.post(
        "/jobs/probe_09_pv_job/submit/veeva_promomats",
        headers={"X-Tenant-Id": "tenant_a", "X-Actor-Id": "alice@tenant_a.com"},
        params={"actor_human": "alice@tenant_a.com", "idempotency_key": "idem_pv_10"},
    )
    results["probe_10_pv_submit_hold"] = {
        "probe_number": 10,
        "passed": pv_submit_resp.status_code in (403, 409)
        and main_mod.JOBS_DB.get("probe_09_pv_job").state == JobLifecycleState.HOLD,
        "status_code": pv_submit_resp.status_code,
    }

    # 11. PV paraphrase: Paraphrased adverse event narrative must be flagged
    job_p11 = _make_job("probe_11")
    pv_p11 = screen_pharmacovigilance_cv08(
        job=job_p11,
        intake_text="My mother developed acute liver failure after starting Aurenix yesterday.",
    )
    results["probe_11_pv_paraphrase"] = {
        "probe_number": 11,
        "passed": pv_p11["cv08_flagged"] is True and job_p11.pv_icrs_flagged is True,
        "cv08_flagged": pv_p11["cv08_flagged"],
    }

    # 12. PV context: Quoted training phrase 'patient experienced' must NOT be flagged
    job_p12 = _make_job("probe_12")
    pv_p12 = screen_pharmacovigilance_cv08(
        job=job_p12,
        intake_text="For internal MLR training policy: the quoted marker phrase 'patient experienced' is an example trigger.",
    )
    results["probe_12_pv_context"] = {
        "probe_number": 12,
        "passed": pv_p12["cv08_flagged"] is False and job_p12.pv_icrs_flagged is False,
        "cv08_flagged": pv_p12["cv08_flagged"],
    }

    # 13. Missing submission mandate: Empty mandate_allowed_actions = [] must block upload
    job_p13 = _make_job("probe_13", mandate=[])
    ingest_working_document(job=job_p13, body_text="Draft copy.", actor="alice@tenant_a.com")
    adapter_p13 = CapabilityCheckedPlatformAdapter("veeva_promomats")
    mandate_blocked = False
    try:
        adapter_p13.execute_action(
            job=job_p13,
            action=ConnectorActionType.UPLOAD_VERSION,
            actor_human="alice@tenant_a.com",
            payload={"version": 1},
            idempotency_key="idem_p13",
        )
    except PermissionError:
        mandate_blocked = True
    results["probe_13_missing_submission_mandate"] = {
        "probe_number": 13,
        "passed": mandate_blocked is True and job_p13.state == JobLifecycleState.DRAFT,
        "mandate_blocked": mandate_blocked,
    }

    # 14. Approval evidence: Arbitrary approval_record_id with no approved_version must NOT set approved
    job_p14 = _make_job("probe_14")
    ingest_working_document(job=job_p14, body_text="Draft copy v1.", actor="alice@tenant_a.com")
    adapter_p14 = CapabilityCheckedPlatformAdapter("veeva_promomats")
    adapter_p14.ingest_platform_webhook_event(
        job=job_p14,
        event_id="evt_no_version",
        event_type="status_changed_approved",
        payload={"approval_record_id": "ARBITRARY-ID-ONLY"},
    )
    results["probe_14_approval_evidence"] = {
        "probe_number": 14,
        "passed": job_p14.state != JobLifecycleState.APPROVED and job_p14.approved_handoff_bundle_uri is None,
        "state": job_p14.state.value,
    }

    # 15. Approval after edit: Creating v2 after v1 approval must invalidate approved state and handoff URI
    job_p15 = _make_job("probe_15")
    ingest_working_document(job=job_p15, body_text="Approved copy v1.", actor="alice@tenant_a.com")
    adapter_p14.ingest_platform_webhook_event(
        job=job_p15,
        event_id="evt_valid_v1_approval",
        event_type="status_changed_approved",
        payload={"approval_record_id": "VEEVA-APPR-991", "approved_version": 1},
    )
    was_approved_v1 = job_p15.state == JobLifecycleState.APPROVED
    ingest_working_document(job=job_p15, body_text="Edited copy v2 after approval.", actor="alice@tenant_a.com")
    results["probe_15_approval_after_edit"] = {
        "probe_number": 15,
        "passed": (
            was_approved_v1 is True
            and job_p15.state == JobLifecycleState.DRAFT
            and job_p15.approved_handoff_bundle_uri is None
        ),
        "state_after_v2_edit": job_p15.state.value,
        "handoff_uri_after_v2_edit": job_p15.approved_handoff_bundle_uri,
    }

    # 16. Upload reconciliation: Retry after uncertain drop must update job.state to UPLOADED
    job_p16 = _make_job("probe_16")
    ingest_working_document(job=job_p16, body_text="Upload reconciliation test.", actor="alice@tenant_a.com")
    adapter_p16 = CapabilityCheckedPlatformAdapter("veeva_promomats")
    adapter_p16.execute_action(
        job=job_p16,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="alice@tenant_a.com",
        payload={"v": 1},
        idempotency_key="idem_p16",
        simulate_uncertain_network_drop=True,
    )
    rcpt_p16 = adapter_p16.execute_action(
        job=job_p16,
        action=ConnectorActionType.UPLOAD_VERSION,
        actor_human="alice@tenant_a.com",
        payload={"v": 1},
        idempotency_key="idem_p16",
        simulate_uncertain_network_drop=False,
    )
    results["probe_16_upload_reconciliation"] = {
        "probe_number": 16,
        "passed": (
            rcpt_p16.status == "reconciled_no_duplicate"
            and job_p16.state == JobLifecycleState.UPLOADED
        ),
        "receipt_status": rcpt_p16.status,
        "job_state": job_p16.state.value,
    }

    # 17. Idempotency scope: Another tenant/job reusing same idempotency_key must be rejected
    job_p17_other = _make_job("probe_17_other", tenant_id="tenant_b")
    ingest_working_document(job=job_p17_other, body_text="Other tenant doc.", actor="bob@tenant_b.com")
    scope_collision_blocked = False
    try:
        adapter_p16.execute_action(
            job=job_p17_other,
            action=ConnectorActionType.UPLOAD_VERSION,
            actor_human="bob@tenant_b.com",
            payload={"v": 1},
            idempotency_key="idem_p16",
        )
    except PermissionError:
        scope_collision_blocked = True
    results["probe_17_idempotency_scope"] = {
        "probe_number": 17,
        "passed": scope_collision_blocked is True,
        "collision_blocked": scope_collision_blocked,
    }

    # 18. Incremental comments: Importing c1 then c2 must preserve both c1 and c2
    job_p18 = _make_job("probe_18")
    c1 = ReviewComment(
        comment_id="cmt_inc_1",
        reviewer_name="Reviewer 1",
        reviewer_role="Medical",
        timestamp="2026-10-28T10:00:00Z",
        anchored_passage="Sentence A.",
        comment_text="First batch comment.",
        category=CommentCategory.FACTUAL_CORRECTION,
    )
    c2 = ReviewComment(
        comment_id="cmt_inc_2",
        reviewer_name="Reviewer 2",
        reviewer_role="Legal",
        timestamp="2026-10-28T10:05:00Z",
        anchored_passage="Sentence B.",
        comment_text="Second batch comment.",
        category=CommentCategory.EDITORIAL_REQUEST,
    )
    import_and_reconcile_comments(job=job_p18, raw_comments=[c1])
    import_and_reconcile_comments(job=job_p18, raw_comments=[c2])
    results["probe_18_incremental_comments"] = {
        "probe_number": 18,
        "passed": len(job_p18.comments) == 2 and {c.comment_id for c in job_p18.comments} == {"cmt_inc_1", "cmt_inc_2"},
        "comment_Ids": [c.comment_id for c in job_p18.comments],
    }

    # 19. Overlapping conflict: Different anchored_passage strings targeting overlapping text must be flagged as conflict
    job_p19 = _make_job("probe_19")
    ov1 = ReviewComment(
        comment_id="cmt_ov_1",
        reviewer_name="Medical Reviewer",
        reviewer_role="Medical",
        timestamp="2026-10-28T11:00:00Z",
        anchored_passage="In AURORA-301, Aurenix reduced risk by 38% in 2L+ NSCLC.",
        comment_text="Add HR 0.62 to risk reduction clause.",
        category=CommentCategory.FACTUAL_CORRECTION,
        proposed_spans=[
            WordingSpanEdit(
                target_text="reduced risk by 38%",
                replacement_text="reduced risk by 38% (HR 0.62)",
            )
        ],
    )
    ov2 = ReviewComment(
        comment_id="cmt_ov_2",
        reviewer_name="Regulatory Reviewer",
        reviewer_role="Regulatory",
        timestamp="2026-10-28T11:03:00Z",
        anchored_passage="reduced risk by 38%",
        comment_text="Replace percentage with qualitative phrasing.",
        category=CommentCategory.POLICY_QUESTION,
        proposed_spans=[
            WordingSpanEdit(
                target_text="reduced risk by 38%",
                replacement_text="demonstrated a statistically significant reduction",
            )
        ],
    )
    ov_res = import_and_reconcile_comments(job=job_p19, raw_comments=[ov1, ov2])
    results["probe_19_overlapping_conflict"] = {
        "probe_number": 19,
        "passed": (
            ov_res["requires_human_resolution"] is True
            and len(ov_res["conflict_groups"]) == 1
            and all(c.human_decision == "escalated" for c in job_p19.comments)
        ),
        "conflict_groups": ov_res["conflict_groups"],
    }

    # 20. Atomic rollback control: Second missing span rolls back first edit; body unchanged
    job_p20 = _make_job("probe_20")
    base_p20 = "First sentence is present. Second sentence is present."
    ingest_working_document(job=job_p20, body_text=base_p20, actor="alice@tenant_a.com")
    cmt_p20 = ReviewComment(
        comment_id="cmt_p20",
        reviewer_name="Reviewer",
        reviewer_role="Medical",
        timestamp="2026-10-28T12:00:00Z",
        anchored_passage=base_p20,
        comment_text="Two-part edit.",
        category=CommentCategory.FACTUAL_CORRECTION,
        human_decision="accepted",
        proposed_spans=[
            WordingSpanEdit(target_text="First sentence is present.", replacement_text="First mutated."),
            WordingSpanEdit(target_text="Missing span text.", replacement_text="Second mutated."),
        ],
    )
    res_p20 = apply_accepted_changes(job=job_p20, comments=[cmt_p20], human_actor="alice@tenant_a.com")
    results["probe_20_atomic_rollback_control"] = {
        "probe_number": 20,
        "passed": res_p20["updated_body"] == base_p20 and cmt_p20.placement_status == "unplaced",
        "body_unchanged": res_p20["updated_body"] == base_p20,
    }

    # 21. Pending-comment control: Pending comment does not edit body
    job_p21 = _make_job("probe_21")
    ingest_working_document(job=job_p21, body_text=base_p20, actor="alice@tenant_a.com")
    cmt_p21 = ReviewComment(
        comment_id="cmt_p21",
        reviewer_name="Reviewer",
        reviewer_role="Medical",
        timestamp="2026-10-28T12:05:00Z",
        anchored_passage=base_p20,
        comment_text="Pending edit.",
        category=CommentCategory.EDITORIAL_REQUEST,
        human_decision="pending",
        proposed_spans=[
            WordingSpanEdit(target_text="First sentence is present.", replacement_text="Should not apply.")
        ],
    )
    res_p21 = apply_accepted_changes(job=job_p21, comments=[cmt_p21], human_actor="alice@tenant_a.com")
    results["probe_21_pending_comment_control"] = {
        "probe_number": 21,
        "passed": res_p21["updated_body"] == base_p20 and len(res_p21["placed_comment_Ids"]) == 0,
        "body_unchanged": res_p21["updated_body"] == base_p20,
    }

    # 22. Inherited model wording: Model sentence carried forward in later human revision must NOT become a human fact
    init_p22 = "Baseline sentence one."
    revs_p22 = [
        AssetRevisionSnapshot(
            revision_id="r1_model",
            version_number=1,
            body="Baseline sentence one. Model generated sentence two.",
            sha256_hash="h1",
            event_type=ProvenanceEventType.ASSET_DRAFTED,
            actor="gemini-3.8-flash",
        ),
        AssetRevisionSnapshot(
            revision_id="r2_human",
            version_number=2,
            body="Baseline sentence one. Model generated sentence two. Human added sentence three.",
            sha256_hash="h2",
            event_type=ProvenanceEventType.ASSET_EDIT_RECORDED,
            actor="alice@tenant_a.com",
        ),
    ]
    facts_p22 = extract_human_resolved_facts(initial_payload_body=init_p22, revisions=revs_p22)
    results["probe_22_inherited_model_wording"] = {
        "probe_number": 22,
        "passed": facts_p22 == ["Human added sentence three."],
        "extracted_facts": facts_p22,
    }

    # 23. Future/unverified reuse: Unrelated, unverified, future-effective record must have can_reuse_directly=False
    lib_p23 = [
        StatementReuseRecord(
            statement_id="stmt_future_unverified",
            tenant_id="tenant_a",
            exact_wording="CardioPulse catheter achieved 94% acute isolation.",
            source_document_id="SRC-1",
            source_version="1.0",
            source_location="Page 1",
            originating_material_id="MAT-1",
            reviewer_role="Medical",
            reviewer_rationale="Future draft.",
            product_or_device_version="Aurenix-100mg",
            sector=SectorType.PHARMA_APPROVED,
            jurisdiction="US",
            audience="HCP",
            channel="Congress Deck",
            purpose="Exchange",
            valid_from="2027-06-01",
            valid_until="2028-06-01",
            evidence_support=ReuseDimensionEvidence.UNVERIFIED,
            public_disclosure=ReuseDimensionDisclosure.INTERNAL_CONFIDENTIAL,
            review_decision=ReuseDimensionDecision.CLEARED_IN_CONTEXT,
            permitted_contexts=["US:HCP:Congress Deck"],
        )
    ]
    reuse_p23 = query_statement_reuse_library(
        library=lib_p23,
        requesting_tenant_id="tenant_a",
        query_text="Aurenix psoriasis progression-free survival",
        target_jurisdiction="US",
        target_audience="HCP",
        target_channel="Congress Deck",
        target_product_version="Aurenix-100mg",
        as_of_date="2026-10-28",
    )
    results["probe_23_future_unverified_reuse"] = {
        "probe_number": 23,
        "passed": (
            reuse_p23[0]["can_reuse_directly"] is False
            and len(reuse_p23[0]["reasons_requiring_rereview"]) >= 3
        ),
        "can_reuse_directly": reuse_p23[0]["can_reuse_directly"],
        "reasons": reuse_p23[0]["reasons_requiring_rereview"],
    }

    # 24. Indication change: Disease A -> Disease B with same population must require new review path
    delta_p24 = analyze_approval_day_label_delta(
        prepared_scenarios=[
            {
                "scenario_id": "scen_disease_a",
                "indication": "Treatment of adult patients with moderate-to-severe plaque psoriasis",
                "population": "adult patients",
                "boxed_warning": None,
            }
        ],
        final_approved_label={
            "indication": "Treatment of adult patients with psoriatic arthritis",
            "population": "adult patients",
            "boxed_warning": None,
        },
        launch_kit_assets=[
            {
                "asset_id": "asset_pr_disease_a",
                "title": "Launch PR",
                "channel": "Press Release",
                "body": "Aurenix is approved for adult patients with moderate-to-severe plaque psoriasis.",
            }
        ],
    )
    results["probe_24_indication_change"] = {
        "probe_number": 24,
        "passed": (
            delta_p24["fits_prepared_scenario"] is False
            and delta_p24["new_review_path_required"] is True
            and len(delta_p24["affected_assets"]) == 1
        ),
        "fits_prepared_scenario": delta_p24["fits_prepared_scenario"],
        "new_review_path_required": delta_p24["new_review_path_required"],
    }

    # 25. Model disagreement: Stub returning contradicted must override heuristic supported
    class DisagreementStubRouter(GeminiModelRouter):
        def create_interaction(self, **kwargs: Any) -> tuple[Any, ModelCallTelemetry]:
            telemetry = ModelCallTelemetry(
                model="gemini-4-argon",
                task_type=kwargs.get("task_type", "claim_verification"),
                interaction_id="int_stub_contradicted",
            )
            return {"status": "contradicted"}, telemetry

    job_p25 = _make_job("probe_25")
    ingest_working_document(
        job=job_p25,
        body_text="Aurenix reduced flaring events by 34% at Week 16.",
        actor="alice@tenant_a.com",
    )
    claims_p25 = locate_and_anchor_evidence(
        job=job_p25,
        source_pack=[
            {
                "source_id": "SRC-P25",
                "version": "1.0",
                "verbatim_text": "Aurenix reduced flaring events by 34% at Week 16.",
                "keywords": ["aurenix", "34%"],
                "locked_numbers": ["34%"],
            }
        ],
        router=DisagreementStubRouter(),
    )
    results["probe_25_model_disagreement"] = {
        "probe_number": 25,
        "passed": claims_p25[0].support_status == SupportClassification.CONTRADICTED,
        "observed": claims_p25[0].support_status.value,
    }

    # 26. Provider failure: Exception in model provider must NOT silently return supported
    class FailingProviderStubRouter(GeminiModelRouter):
        def create_interaction(self, **kwargs: Any) -> tuple[Any, ModelCallTelemetry]:
            raise RuntimeError("Upstream Vertex AI / Gemini endpoint 503 Unavailable")

    job_p26 = _make_job("probe_26")
    ingest_working_document(
        job=job_p26,
        body_text="Aurenix reduced flaring events by 34% at Week 16.",
        actor="alice@tenant_a.com",
    )
    claims_p26 = locate_and_anchor_evidence(
        job=job_p26,
        source_pack=[
            {
                "source_id": "SRC-P26",
                "version": "1.0",
                "verbatim_text": "Aurenix reduced flaring events by 34% at Week 16.",
                "keywords": ["aurenix", "34%"],
                "locked_numbers": ["34%"],
            }
        ],
        router=FailingProviderStubRouter(),
    )
    results["probe_26_provider_failure"] = {
        "probe_number": 26,
        "passed": claims_p26[0].support_status == SupportClassification.UNRESOLVED,
        "observed": claims_p26[0].support_status.value,
        "diagnosis": claims_p26[0].plain_english_diagnosis,
    }

    # 27. State reinitialization: Reloading backend.main must retain persisted jobs via SQLite DurableJobStore
    reloaded_main = importlib.reload(main_mod)
    retained_job = reloaded_main.JOBS_DB.get("probe_07_job")
    results["probe_27_state_reinitialization"] = {
        "probe_number": 27,
        "passed": retained_job is not None and retained_job.tenant_id == "tenant_a",
        "retained_after_reload": retained_job is not None,
    }

    return results


if __name__ == "__main__":
    import sys

    probe_results = run_all_27_adversarial_probes()
    passed_count = sum(1 for v in probe_results.values() if v["passed"])
    print(f"MLR Genie Independent Audit Adversarial Probe Suite ({passed_count}/{len(probe_results)} Passed):")
    print("=" * 88)
    for key, val in probe_results.items():
        badge = "PASS" if val["passed"] else "FAIL"
        print(f"[{badge}] #{val['probe_number']:02d} {key}")
    print("=" * 88)
    sys.exit(0 if passed_count == len(probe_results) else 1)
