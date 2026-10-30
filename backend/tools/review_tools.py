from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from backend.agent.models_router import GeminiModelRouter
from backend.domain.schemas import (
    AdvisoryFinding,
    CommentCategory,
    JobLifecycleState,
    JobRecord,
    ProvenanceEventType,
    ReuseDimensionDecision,
    ReuseDimensionDisclosure,
    ReuseDimensionEvidence,
    ReviewComment,
    SectorType,
    StatementReuseRecord,
)


_QUOTED_PHRASE_RE = re.compile(r"(['\"])(?:(?=(\\?))\2.)*?\1")
_META_TRAINING_CONTEXT_RE = re.compile(
    r"\b(training|example|quoted|phrase|policy|checklist|guideline|test case|do not use the phrase|marker)\b",
    flags=re.IGNORECASE,
)
_ICSR_PATIENT_SUBJECT_RE = re.compile(
    r"\b(patient|mother|father|husband|wife|son|daughter|child|teenager|man|woman|"
    r"\d+-year-old|consumer|customer|caller|individual|my\s+(?:mom|dad|mother|father|husband|wife|child|son|daughter))\b",
    flags=re.IGNORECASE,
)
_ICSR_DRUG_EXPOSURE_RE = re.compile(
    r"\b(after\s+(?:starting|taking|receiving|using|dose|injection|infusion)|"
    r"started\s+(?:taking|on)|while\s+taking|taking\s+aurenix|on\s+aurenix|dose\s+\d+|"
    r"administered|prescribed)\b",
    flags=re.IGNORECASE,
)
_ICSR_ADVERSE_EVENT_RE = re.compile(
    r"\b(liver\s+failure|hepatic\s+failure|renal\s+failure|hospitalized|hospitalisation|"
    r"anaphylaxis|anaphylactic|seizure|severe\s+nausea|acute\s+jaundice|jaundice|syncope|"
    r"stroke|myocardial\s+infarction|heart\s+attack|emergency\s+room|icu|died|death|"
    r"severe\s+rash|stevens-johnson|adverse\s+event\s+report|chest\s+pain|arrhythmia)\b",
    flags=re.IGNORECASE,
)


def screen_pharmacovigilance_cv08(
    *,
    job: JobRecord,
    intake_text: str,
    router: GeminiModelRouter | None = None,
) -> dict[str, Any]:
    """
    24-Hour Pharmacovigilance (CV-08) Boundary (via `gemini-3.5-flash-lite`):
    Individual patient adverse event reports (ICSRs) must never be ingested as comms copy;
    they must be flagged and routed to Pharmacovigilance within 24 hours.
    Distinguishes real/paraphrased ICSR narratives from meta/quoted training examples.
    """
    active_router = router or GeminiModelRouter()

    # Strip quoted phrases when text is discussing training/policy/examples (Probe 12)
    unquoted_text = _QUOTED_PHRASE_RE.sub(" ", intake_text)
    scan_text = unquoted_text if _META_TRAINING_CONTEXT_RE.search(intake_text) else intake_text
    lower = scan_text.lower()

    icsr_markers = [
        "patient experienced",
        "hospitalized after taking",
        "adverse event report",
        "reported severe nausea after",
        "anaphylaxis in a 64-year-old",
    ]
    matched_marker = next((m for m in icsr_markers if m in lower), None)

    # Structured paraphrase detection: [identifiable patient/person] + [drug exposure] + [adverse clinical event] (Probe 11)
    paraphrase_icsr = bool(
        _ICSR_PATIENT_SUBJECT_RE.search(scan_text)
        and _ICSR_DRUG_EXPOSURE_RE.search(scan_text)
        and _ICSR_ADVERSE_EVENT_RE.search(scan_text)
    )
    flagged = (matched_marker is not None) or paraphrase_icsr

    active_router.create_interaction(
        task_type="cv08_pv_gate",
        prompt=f"Screen intake for CV-08 ICSR: {intake_text[:200]}",
        system_instruction="Screen for individual patient adverse event reports (CV-08).",
        fallback_payload={"flagged": flagged},
    )
    if flagged:
        job.pv_icrs_flagged = True
        job.state = JobLifecycleState.HOLD
        job.pv_routing_note = (
            "Regulatory Stop (CV-08): Individual patient adverse event detected. "
            "Do not use as communications copy; route immediately to Pharmacovigilance within 24 hours."
        )
        return {
            "cv08_flagged": True,
            "routing_note": job.pv_routing_note,
            "model_used": "gemini-3.5-flash-lite",
        }
    return {
        "cv08_flagged": False,
        "routing_note": None,
        "model_used": "gemini-3.5-flash-lite",
    }


def validate_strategy(*, job: JobRecord, text_to_check: str) -> list[AdvisoryFinding]:
    """
    Mandatory Rule 1 (Invariant 10 — Heuristics Advise, Humans Decide; No Blocking Regexes):
    Never throws or blocks execution; returns advisory findings with `severity="note"`
    and plain-English communications diagnostics.
    """
    findings: list[AdvisoryFinding] = []
    lower = text_to_check.lower()
    advisory_phrases = {
        "best-in-class": (
            "Uses comparative phrasing 'best-in-class' without head-to-head clinical trial citations. "
            "Consider restating with the specific trial endpoint and population."
        ),
        "guaranteed": (
            "Uses absolute assurance wording 'guaranteed'. MLR reviewers typically request "
            "qualified clinical outcome language tied to the study population."
        ),
    }
    for phrase, guidance in advisory_phrases.items():
        if phrase in lower:
            findings.append(
                AdvisoryFinding(
                    code=f"ADVISORY_{phrase.upper().replace('-', '_')}",
                    message=guidance,
                    affected_text=phrase,
                    PlainEnglishDiagnosis=guidance,
                    severity="note",
                )
            )
    job.advisory_findings = findings
    return findings


def query_statement_reuse_library(
    *,
    library: list[StatementReuseRecord],
    requesting_tenant_id: str,
    query_text: str,
    target_jurisdiction: str,
    target_audience: str,
    target_channel: str,
    target_product_version: str,
    as_of_date: str = "2026-10-28",
    router: GeminiModelRouter | None = None,
) -> list[dict[str, Any]]:
    """
    Section 4 & Section 11 Acceptance Cases 3 & 12:
    - Enforces strict tenant isolation (blocks cross-client retrieval).
    - Evaluates all 4 separate dimensions (`evidence_support`, `public_disclosure`,
      `review_decision`, `permitted_contexts`), expiry date, and product/device version.
    """
    active_router = router or GeminiModelRouter()
    results: list[dict[str, Any]] = []
    check_dt = datetime.fromisoformat(as_of_date)
    query_vec = active_router.embed_multimodal(
        query_text,
        dimensions=16,
        task_type="RETRIEVAL_QUERY",
    )

    for rec in library:
        if rec.tenant_id != requesting_tenant_id:
            # Cross-tenant records are strictly invisible/blocked
            continue

        valid_from_dt = datetime.fromisoformat(rec.valid_from)
        valid_until_dt = datetime.fromisoformat(rec.valid_until)
        is_not_yet_effective = check_dt < valid_from_dt
        is_expired = check_dt > valid_until_dt
        jurisdiction_matches = rec.jurisdiction.lower() == target_jurisdiction.lower()
        audience_matches = rec.audience.lower() == target_audience.lower()
        channel_matches = rec.channel.lower() == target_channel.lower()
        version_matches = rec.product_or_device_version.lower() == target_product_version.lower()
        context_key = f"{target_jurisdiction}:{target_audience}:{target_channel}".lower()
        context_permitted = any(c.lower() == context_key for c in rec.permitted_contexts)

        doc_vec = (
            rec.embedding_vector
            if len(rec.embedding_vector) == len(query_vec)
            else active_router.embed_multimodal(
                rec.exact_wording,
                dimensions=len(query_vec),
                task_type="RETRIEVAL_DOCUMENT",
            )
        )
        semantic_similarity = round(sum(a * b for a, b in zip(query_vec, doc_vec)), 4)

        # Semantic / lexical overlap check on query_text (Probe 23)
        query_tokens = {
            tok.lower()
            for tok in re.findall(r"[a-zA-Z0-9%]+", query_text)
            if len(tok) >= 3
        }
        wording_tokens = {
            tok.lower()
            for tok in re.findall(r"[a-zA-Z0-9%]+", rec.exact_wording)
            if len(tok) >= 3
        }
        query_matches = (not query_tokens) or bool(query_tokens & wording_tokens)

        reasons_requiring_rereview: list[str] = []
        if not query_matches:
            reasons_requiring_rereview.append(
                "Query text has no topical or lexical overlap with stored statement wording"
            )
        if is_not_yet_effective:
            reasons_requiring_rereview.append(
                f"Statement is future-dated and not effective until {rec.valid_from}"
            )
        if is_expired:
            reasons_requiring_rereview.append(f"Prior approval expired on {rec.valid_until}")
        if rec.evidence_support != ReuseDimensionEvidence.VERIFIED_SOURCE:
            reasons_requiring_rereview.append(
                f"Evidence support is '{rec.evidence_support.value}' (requires verified_source)"
            )
        if rec.public_disclosure == ReuseDimensionDisclosure.INTERNAL_CONFIDENTIAL:
            reasons_requiring_rereview.append(
                "Statement is internal_confidential and not cleared for external reuse"
            )
        if not jurisdiction_matches:
            reasons_requiring_rereview.append(
                f"Jurisdiction changed from {rec.jurisdiction} to {target_jurisdiction} (global approval does not establish local approval)"
            )
        if not audience_matches or not channel_matches or not context_permitted:
            reasons_requiring_rereview.append(
                f"Context changed from ({rec.jurisdiction}, {rec.audience}, {rec.channel}) to ({target_jurisdiction}, {target_audience}, {target_channel})"
            )
        if not version_matches:
            reasons_requiring_rereview.append(
                f"Product/device version changed from {rec.product_or_device_version} to {target_product_version}"
            )
        if rec.review_decision not in (
            ReuseDimensionDecision.CLEARED_IN_CONTEXT,
            ReuseDimensionDecision.ADOPTED_HOUSE_RULE,
        ):
            reasons_requiring_rereview.append(
                f"Decision status is '{rec.review_decision.value}' (public disclosure or single-comment ruling does not grant universal reuse permission)"
            )

        can_reuse_without_new_review = len(reasons_requiring_rereview) == 0
        results.append(
            {
                "statement_id": rec.statement_id,
                "exact_wording": rec.exact_wording,
                "can_reuse_directly": can_reuse_without_new_review,
                "semantic_similarity": semantic_similarity,
                "dimensions": {
                    "evidence_support": rec.evidence_support.value,
                    "public_disclosure": rec.public_disclosure.value,
                    "review_decision": rec.review_decision.value,
                    "permitted_contexts": rec.permitted_contexts,
                },
                "reasons_requiring_rereview": reasons_requiring_rereview,
                "reviewer_rationale": rec.reviewer_rationale,
            }
        )
    return results



def _comments_overlap_or_share_target(c1: ReviewComment, c2: ReviewComment) -> bool:
    """
    Returns True if c1 and c2 target the same sentence ID, overlapping anchored passages,
    or overlapping proposed target spans (Probe 19).
    """
    if c1.target_sentence_id and c2.target_sentence_id and c1.target_sentence_id == c2.target_sentence_id:
        return True
    p1 = c1.anchored_passage.strip()
    p2 = c2.anchored_passage.strip()
    if p1 and p2 and (p1 == p2 or p1 in p2 or p2 in p1):
        return True
    for s1 in c1.proposed_spans:
        t1 = s1.target_text.strip()
        if not t1:
            continue
        if p2 and (t1 in p2 or p2 in t1):
            return True
        for s2 in c2.proposed_spans:
            t2 = s2.target_text.strip()
            if t2 and (t1 == t2 or t1 in t2 or t2 in t1):
                return True
    for s2 in c2.proposed_spans:
        t2 = s2.target_text.strip()
        if t2 and p1 and (t2 in p1 or p1 in t2):
            return True
    return False


def _comments_have_conflicting_edits(c1: ReviewComment, c2: ReviewComment) -> bool:
    """Returns True if two overlapping comments propose incompatible edits or instructions."""
    sig1 = tuple((s.target_text.strip(), s.replacement_text.strip()) for s in c1.proposed_spans)
    sig2 = tuple((s.target_text.strip(), s.replacement_text.strip()) for s in c2.proposed_spans)
    if sig1 and sig2:
        return sig1 != sig2
    return c1.comment_text.strip() != c2.comment_text.strip()


def import_and_reconcile_comments(
    *,
    job: JobRecord,
    raw_comments: list[ReviewComment],
    router: GeminiModelRouter | None = None,
) -> dict[str, Any]:
    """
    Section 3.F & Section 11 Acceptance Case 4:
    - Incrementally merges newly imported comments into `job.comments` by `comment_id` (Probe 18).
    - Detects conflicting instructions targeting the same or overlapping passage/sentence/span
      across reviewers (Probe 19) and escalates conflicts to a human decision-maker.
    """
    active_router = router or GeminiModelRouter()

    # Incremental merge by comment_id (Probe 18)
    merged_by_id: dict[str, ReviewComment] = {c.comment_id: c for c in job.comments}
    for c in raw_comments:
        existing_c = merged_by_id.get(c.comment_id)
        if (
            existing_c is not None
            and existing_c.placement_status == "placed"
            and c.human_decision == "accepted"
        ):
            c.placement_status = "placed"
            c.provenance = existing_c.provenance
            c.human_actor = existing_c.human_actor or c.human_actor
        elif existing_c is None or existing_c.placement_status != "placed":
            if c.human_decision == "accepted" and c.placement_status == "placed":
                c.placement_status = "pending"
        merged_by_id[c.comment_id] = c
    all_comments = list(merged_by_id.values())

    # Reset conflict linkages before full reconciliation across all active comments
    conflict_adj: dict[str, set[str]] = {c.comment_id: set() for c in all_comments}
    n = len(all_comments)
    for i in range(n):
        for j in range(i + 1, n):
            c1, c2 = all_comments[i], all_comments[j]
            if _comments_overlap_or_share_target(c1, c2) and _comments_have_conflicting_edits(c1, c2):
                conflict_adj[c1.comment_id].add(c2.comment_id)
                conflict_adj[c2.comment_id].add(c1.comment_id)

    visited: set[str] = set()
    conflict_groups: list[dict[str, Any]] = []
    comment_map = {c.comment_id: c for c in all_comments}

    for c in all_comments:
        peers = conflict_adj[c.comment_id]
        if not peers or c.comment_id in visited:
            continue
        # BFS connected component of conflicting comments
        comp_Ids: list[str] = []
        queue = [c.comment_id]
        while queue:
            curr = queue.pop(0)
            if curr in visited:
                continue
            visited.add(curr)
            comp_Ids.append(curr)
            for nb in conflict_adj[curr]:
                if nb not in visited:
                    queue.append(nb)

        comp_comments = [comment_map[cid] for cid in comp_Ids]
        for cc in comp_comments:
            cc.category = CommentCategory.CONFLICTING_INSTRUCTION
            cc.conflicts_with_comment_Ids = [oid for oid in comp_Ids if oid != cc.comment_id]
            cc.human_decision = "escalated"

        conflict_groups.append(
            {
                "anchored_passage": comp_comments[0].anchored_passage.strip(),
                "conflicting_comment_Ids": comp_Ids,
                "reviewers": [f"{cc.reviewer_name} ({cc.reviewer_role})" for cc in comp_comments],
                "escalation_note": (
                    "Conflicting reviewer instructions on the same or overlapping passage. "
                    "Escalated to human decision-maker in Decisions Needed; Genie will not guess."
                ),
            }
        )

    active_router.create_interaction(
        task_type="comment_reconciliation",
        prompt=f"Reconcile {len(all_comments)} comments for job {job.job_id}",
        system_instruction="Group comments, preserve verbatim wording, and expose conflicts for human decision.",
        fallback_payload={"imported_count": len(all_comments), "conflicts": len(conflict_groups)},
    )

    job.comments = all_comments
    return {
        "imported_count": len(job.comments),
        "conflict_groups": conflict_groups,
        "requires_human_resolution": len(conflict_groups) > 0,
        "model_used": "gemini-4-argon",
    }


def analyze_approval_day_label_delta(
    *,
    prepared_scenarios: list[dict[str, Any]],
    final_approved_label: dict[str, Any],
    launch_kit_assets: list[dict[str, Any]],
    router: GeminiModelRouter | None = None,
) -> dict[str, Any]:
    """
    Section 6 & Section 11 Acceptance Cases 6, 8, 9:
    - Uses `gemini-4-argon` (Priority Inference tier) to compare draft/scenario expectations
      against the final FDA/regulatory approved label.
    - Exposes narrowed indications, new boxed warnings, or unanticipated outcomes that fall
      outside the pre-reviewed scenario set and computes the multi-asset impact map.
    """
    active_router = router or GeminiModelRouter()
    final_indication = final_approved_label.get("indication", "")
    final_population = final_approved_label.get("population", "")
    final_boxed_warning = final_approved_label.get("boxed_warning")
    unexpected_restriction = final_approved_label.get("unexpected_restriction")
    section_12_1_moa = str(final_approved_label.get("section_12_1_moa") or "").strip()
    section_14_clinical_figures = [
        figure
        for figure in (final_approved_label.get("section_14_clinical_figures") or [])
        if figure.get("old_value") != figure.get("new_value")
    ]

    def _normalized_wording(text: str) -> str:
        return " ".join(text.split()).casefold()

    def _indication_matches_scenario(sc: dict[str, Any]) -> bool:
        sc_ind = str(sc.get("indication", "")).strip()
        if not final_indication:
            return not sc_ind
        if sc_ind:
            return sc_ind.lower() == final_indication.strip().lower()
        return True

    matched_scenario = next(
        (
            sc
            for sc in prepared_scenarios
            if sc.get("population", "").lower() == final_population.lower()
            and _indication_matches_scenario(sc)
            and sc.get("boxed_warning") == final_boxed_warning
            and not unexpected_restriction
            and (
                not section_12_1_moa
                or _normalized_wording(str(sc.get("section_12_1_moa") or ""))
                == _normalized_wording(section_12_1_moa)
            )
            and (
                not section_14_clinical_figures
                or sc.get("section_14_clinical_figures") == section_14_clinical_figures
            )
        ),
        None,
    )

    _INDICATION_STOPWORDS = {
        "treatment",
        "indicated",
        "patients",
        "adult",
        "adults",
        "adolescent",
        "adolescents",
        "years",
        "older",
        "aged",
        "with",
        "after",
        "approved",
        "for",
        "the",
        "and",
        "who",
        "are",
        "candidates",
        "moderate",
        "severe",
    }
    indication_keywords = [
        w.lower()
        for w in re.findall(r"[a-zA-Z0-9+-]+", final_indication)
        if len(w) >= 3 and w.lower() not in _INDICATION_STOPWORDS
    ]

    affected_assets: list[dict[str, Any]] = []
    for asset in launch_kit_assets:
        body = asset.get("body", "")
        impact_reasons: list[str] = []
        if final_indication and indication_keywords:
            if not any(kw in body.lower() for kw in indication_keywords):
                impact_reasons.append(
                    f"Final approved indication '{final_indication}' does not match indication in asset"
                )
        if final_population and final_population.lower() not in body.lower():
            impact_reasons.append(f"Narrowed final population '{final_population}' missing or overstated")
        if final_boxed_warning and final_boxed_warning.lower() not in body.lower():
            impact_reasons.append(f"New boxed warning '{final_boxed_warning}' must be added to safety block")
        if unexpected_restriction:
            impact_reasons.append(f"Unanticipated regulatory restriction: {unexpected_restriction}")
        if section_12_1_moa and _normalized_wording(section_12_1_moa) not in _normalized_wording(body):
            impact_reasons.append(
                f"Section 12.1 Mechanism of Action update required: '{section_12_1_moa}'"
            )
        for figure in section_14_clinical_figures:
            old_value = figure.get("old_value")
            old_text = str(old_value).strip() if old_value is not None else ""
            # Match the supplied value literally, not a substring of another word/number.
            if old_text and re.search(rf"(?<![\w.]){re.escape(old_text)}(?!\w|\.\d)", body):
                endpoint = figure.get("endpoint") or "clinical figure"
                impact_reasons.append(
                    f"Section 14 update ({endpoint}): replace '{old_text}' with '{figure.get('new_value')}'"
                )
        if impact_reasons:
            affected_assets.append(
                {
                    "asset_id": asset["asset_id"],
                    "title": asset.get("title", asset["asset_id"]),
                    "channel": asset.get("channel", "Press Release"),
                    "impact_reasons": impact_reasons,
                    "requires_human_reapproval": True,
                }
            )

    if affected_assets and matched_scenario is not None and not matched_scenario.get("indication"):
        # If scenario omitted indication but the final indication diverges from the launch kit assets (Probe 24)
        if any("indication" in r.lower() for a in affected_assets for r in a["impact_reasons"]):
            matched_scenario = None

    _, telemetry = active_router.create_interaction(
        task_type="approval_day_label_delta",
        prompt=f"Compare final indication '{final_indication}' against {len(prepared_scenarios)} scenarios.",
        system_instruction="Identify out-of-scenario label changes and affected launch kit assets.",
        fallback_payload={"in_scenario": matched_scenario is not None},
    )

    return {
        "fits_prepared_scenario": matched_scenario is not None,
        "matched_scenario_id": matched_scenario["scenario_id"] if matched_scenario else None,
        "out_of_scenario_reasons": (
            []
            if matched_scenario is not None
            else [
                r
                for r in [
                    f"Final indication changed to '{final_indication}'" if final_indication else None,
                    f"Final population narrowed to '{final_population}'" if final_population else None,
                    f"New boxed warning: '{final_boxed_warning}'" if final_boxed_warning else None,
                    f"Unanticipated outcome: '{unexpected_restriction}'" if unexpected_restriction else None,
                    f"Section 12.1 MoA update: '{section_12_1_moa}'" if section_12_1_moa else None,
                    (
                        f"Section 14 clinical figure updates ({len(section_14_clinical_figures)} item(s))"
                        if section_14_clinical_figures
                        else None
                    ),
                ]
                if r is not None
            ]
        ),
        "affected_assets": affected_assets,
        "new_review_path_required": matched_scenario is None or len(affected_assets) > 0,
        "model_used": telemetry.model,
        "service_tier": telemetry.service_tier,
    }


def compute_multi_asset_blast_radius(
    *,
    updated_source_id: str,
    old_value: str,
    new_value: str,
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Section 4 & Section 11 Acceptance Case 6:
    Answers 'Which materials still contain the old number?' when a source document updates.
    """
    impacted: list[dict[str, Any]] = []
    for asset in assets:
        body = asset.get("body", "")
        cites_source = updated_source_id in asset.get("source_Ids", [])
        contains_old = old_value in body
        if cites_source or contains_old:
            impacted.append(
                {
                    "asset_id": asset["asset_id"],
                    "title": asset.get("title", asset["asset_id"]),
                    "contains_stale_value": contains_old,
                    "stale_value": old_value if contains_old else None,
                    "replacement_candidate": new_value,
                }
            )
    return impacted
