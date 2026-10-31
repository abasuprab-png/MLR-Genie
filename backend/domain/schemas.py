from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, PrivateAttr, model_validator
from backend.domain.assessments import VerifiedSafetyFinding


class SectorType(str, Enum):
    PHARMA_PREAPPROVAL = "pharma_preapproval"
    PHARMA_APPROVED = "pharma_approved"
    BIOTECH = "biotech"
    MEDTECH = "medtech"
    CORPORATE = "corporate"


class JobLifecycleState(str, Enum):
    """Distinct workflow states (Section 3: exported != submitted != uploaded != approved != released)."""

    DRAFT = "draft"
    HOLD = "hold"
    SUBMISSION_READY = "submission_ready"
    UPLOADED = "uploaded"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    RELEASED = "released"


class SupportClassification(str, Enum):
    """Claim-to-source verification status (Section 3.C)."""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"


class CommentCategory(str, Enum):
    """Reviewer comment classification (Section 3.F)."""

    FACTUAL_CORRECTION = "factual_correction"
    MISSING_SUPPORT = "missing_support"
    POLICY_QUESTION = "policy_question"
    CONFLICTING_INSTRUCTION = "conflicting_instruction"
    EDITORIAL_REQUEST = "editorial_request"


class ProvenanceEventType(str, Enum):
    """Invariant 1 — Strict Human vs. Model Provenance."""

    ASSET_DRAFTED = "AssetDrafted"           # Model-generated proposal; NEVER classified as leaderSupplied fact
    ASSET_EDIT_RECORDED = "AssetEditRecorded"  # Human-authored / confirmed edit
    HUMAN_CONFIRMED = "HumanConfirmed"       # Explicitly confirmed by R&D/Comms handoff or MLR reviewer


class AdvisoryFinding(BaseModel):
    """Invariant 10 — Heuristics Advise, Humans Decide (severity is always 'note', never blocking)."""

    code: str
    message: str
    affected_text: str
    PlainEnglishDiagnosis: str
    severity: Literal["note"] = "note"


class PdfBoundingBox(BaseModel):
    """Exact PDF coordinate anchor validated against rendered page dimensions."""

    page_number: int = Field(ge=1)
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float = 612.0
    page_height: float = 792.0
    source_document_id: str
    source_version: str
    verbatim_quote: str
    snippet_sha256: str
    anchor_valid: bool = True

    @model_validator(mode="after")
    def _validate_coordinates_and_quote(self) -> "PdfBoundingBox":
        coords_ok = (
            self.page_number >= 1
            and self.page_width > 0.0
            and self.page_height > 0.0
            and 0.0 <= self.x0 < self.x1 <= self.page_width
            and 0.0 <= self.y0 < self.y1 <= self.page_height
        )
        quote_ok = bool(self.verbatim_quote and self.verbatim_quote.strip())
        self.anchor_valid = bool(coords_ok and quote_ok)
        return self


SourceAnchor = PdfBoundingBox


class ClaimVerificationVerdict(BaseModel):
    """
    Strict Pydantic structured-output schema for Gemini 4 Argon claim verification
    (passed via `response_schema=ClaimVerificationVerdict.model_json_schema()`).
    Includes structured PICO clinical entailment decomposition (`population`, `endpoint`,
    `comparator`, `direction`, `magnitude`, `timeframe`) and coordinate/quote grounding.
    """

    status: Literal["supported", "partially_supported", "contradicted", "unresolved"]
    classification: Literal["supported", "partially_supported", "contradicted", "unresolved"]
    entailment_classification: Literal["supported", "partially_supported", "contradicted", "unresolved"]
    population: str
    endpoint: str
    comparator: str
    direction: str
    magnitude: str
    timeframe: str
    population_match: bool
    endpoint_match: bool
    verbatim_quote: str
    source_passage_id: str = Field(description="Exact passage_id of the selected authorized_source_passages entry; empty if none supports the claim.")
    page_number: int
    box_2d: list[int]
    numeric_values_matched: list[str]
    unsupported_numeric_values: list[str]
    numerical_mismatches: list[str]
    missing_qualifiers: list[str]
    rationale: str
    plain_english_rationale: str

    @model_validator(mode="before")
    @classmethod
    def _sync_status_and_classification(cls, data: Any) -> Any:
        if isinstance(data, dict):
            out = dict(data)
            st = out.get("status") or out.get("support_status")
            cl = out.get("classification")
            ec = out.get("entailment_classification")
            resolved_status = st or cl or ec or "unresolved"
            out["status"] = st or resolved_status
            out["classification"] = cl or resolved_status
            out["entailment_classification"] = ec or resolved_status

            for str_field in (
                "population",
                "endpoint",
                "comparator",
                "direction",
                "magnitude",
                "timeframe",
                "verbatim_quote",
                "source_passage_id",
            ):
                if out.get(str_field) is None:
                    out[str_field] = ""

            if out.get("population_match") is None:
                out["population_match"] = True
            if out.get("endpoint_match") is None:
                out["endpoint_match"] = True
            if out.get("page_number") is None:
                out["page_number"] = 1
            if out.get("box_2d") is None:
                out["box_2d"] = [0, 0, 0, 0]

            if out.get("numeric_values_matched") is None:
                out["numeric_values_matched"] = []
            if out.get("missing_qualifiers") is None:
                out["missing_qualifiers"] = []

            unsup = out.get("unsupported_numeric_values")
            num_mis = out.get("numerical_mismatches")
            if unsup is not None and num_mis is None:
                out["numerical_mismatches"] = list(unsup)
            elif num_mis is not None and unsup is None:
                out["unsupported_numeric_values"] = list(num_mis)
            elif unsup is None and num_mis is None:
                out["unsupported_numeric_values"] = []
                out["numerical_mismatches"] = []

            pe_rat = out.get("plain_english_rationale")
            rat = out.get("rationale")
            if pe_rat is not None and rat is None:
                out["rationale"] = str(pe_rat)
            elif rat is not None and pe_rat is None:
                out["plain_english_rationale"] = str(rat)
            elif pe_rat is None and rat is None:
                out["plain_english_rationale"] = ""
                out["rationale"] = ""

            return out
        return data


class ClaimAssessment(BaseModel):
    """Extracted factual assertion and its clinical/regulatory support evaluation."""

    claim_id: str
    sentence_text: str
    paragraph_index: int
    support_status: SupportClassification
    entailment_classification: SupportClassification | None = None
    population: str = ""
    endpoint: str = ""
    comparator: str = ""
    direction: str = ""
    magnitude: str = ""
    timeframe: str = ""
    locked_numbers: list[str] = Field(default_factory=list)
    numerical_mismatches: list[str] = Field(default_factory=list)
    required_qualifiers: list[str] = Field(default_factory=list)
    missing_qualifiers: list[str] = Field(default_factory=list)
    paired_isi_present: bool = True
    requires_isi: bool = True
    plain_english_diagnosis: str = ""
    anchors: list[PdfBoundingBox] = Field(default_factory=list)
    model_used: str = "gemini-4-argon"
    bound_version: int = 1
    bound_revision_sha256: str = ""
    stale: bool = False

    @property
    def plain_english_rationale(self) -> str:
        return self.plain_english_diagnosis


class ReuseDimensionEvidence(str, Enum):
    VERIFIED_SOURCE = "verified_source"
    PARTIAL_SOURCE = "partial_source"
    UNVERIFIED = "unverified"


class ReuseDimensionDisclosure(str, Enum):
    PUBLIC_COMPANY_FOOTPRINT = "public_company_footprint"
    EXTERNAL_PEER_PRECEDENT = "external_peer_precedent"
    INTERNAL_CONFIDENTIAL = "internal_confidential"


class ReuseDimensionDecision(str, Enum):
    CLEARED_IN_CONTEXT = "cleared_in_context"
    SCOPED_RULING = "scoped_ruling"
    ADOPTED_HOUSE_RULE = "adopted_house_rule"
    REJECTED_OR_REPLACED = "rejected_or_replaced"
    AWAITING_REVIEW = "awaiting_review"


class StatementReuseRecord(BaseModel):
    """Section 4 — Reviewed statement & decision library with 4 independent dimensions."""

    statement_id: str
    tenant_id: str
    exact_wording: str
    replaced_or_rejected_wording: str | None = None
    source_document_id: str
    source_version: str
    source_location: str
    originating_material_id: str
    reviewer_role: str
    reviewer_rationale: str
    product_or_device_version: str
    sector: SectorType
    jurisdiction: str
    audience: str
    channel: str
    purpose: str
    language: str = "en-US"
    qualifications: list[str] = Field(default_factory=list)
    valid_from: str
    valid_until: str
    conditions_on_reuse: str = ""
    # The 4 mandatory separate dimensions:
    evidence_support: ReuseDimensionEvidence
    public_disclosure: ReuseDimensionDisclosure
    review_decision: ReuseDimensionDecision
    permitted_contexts: list[str] = Field(default_factory=list)
    provenance: ProvenanceEventType = ProvenanceEventType.HUMAN_CONFIRMED
    confirmed_by: str | None = None
    embedding_model: str = "gemini-embedding-2"
    embedding_vector: list[float] = Field(default_factory=list)


class WordingSpanEdit(BaseModel):
    """Single contiguous span replacement within a comment revision."""

    span_id: str | None = None
    target_text: str
    replacement_text: str


class ReviewComment(BaseModel):
    """Imported reviewer comment with conflict and atomic revision tracking (Section 3.F & 3.G)."""

    comment_id: str
    reviewer_name: str
    reviewer_role: Literal["Medical", "Legal", "Regulatory", "MLR Operations"]
    timestamp: str
    anchored_passage: str
    target_sentence_id: str | None = None
    comment_text: str
    category: CommentCategory
    conflicts_with_comment_Ids: list[str] = Field(default_factory=list)
    proposed_response: str = ""
    proposed_spans: list[WordingSpanEdit] = Field(default_factory=list)
    provenance: ProvenanceEventType = ProvenanceEventType.ASSET_DRAFTED
    human_decision: Literal["pending", "accepted", "rejected", "escalated"] = "pending"
    human_actor: str | None = None
    placement_status: Literal["pending", "placed", "unplaced", "kept_source"] = "pending"


class AssetRevisionSnapshot(BaseModel):
    """Tracks revisions for Invariant 1 (Strict Human vs. Model Provenance)."""

    revision_id: str
    version_number: int
    body: str
    sha256_hash: str
    event_type: ProvenanceEventType
    actor: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ConnectorCapabilityStatus(str, Enum):
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    UNTESTED = "untested"


class ConnectorActionType(str, Enum):
    DISCOVER_CONFIGURATION = "discover_configuration"
    READ_MATERIALS = "read_materials"
    UPLOAD_VERSION = "upload_version"
    ATTACH_REFERENCES = "attach_references"
    REQUEST_REVIEW = "request_review"
    READ_COMMENTS_STATUS = "read_comments_status"
    APPLY_AUTHORIZED_UPDATES = "apply_authorized_updates"
    RETRIEVE_APPROVED_PACKAGE = "retrieve_approved_package"


class ConnectorReceipt(BaseModel):
    """Section 7 — Immutable external platform receipt with mandatory read-back verification."""

    receipt_id: str
    job_id: str
    destination_platform: Literal["veeva_promomats", "aprimo_dam", "aprimo_productivity", "package_mode"]
    action: ConnectorActionType
    capability_status: ConnectorCapabilityStatus
    external_object_id: str | None = None
    external_version: str | None = None
    actor_human: str
    integration_identity: str
    idempotency_key: str
    read_back_verified: bool
    read_back_digest: str | None = None
    status: Literal[
        "succeeded",
        "failed_uncertain",
        "reconciled_no_duplicate",
        "unsupported_limitation",
        "blocked_pv_hold",
        "blocked_missing_mandate",
    ]
    detail: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ClientProfile(BaseModel):
    """Section 3.A — Learned client configuration profile."""

    tenant_id: str
    client_name: str
    sector: SectorType
    primary_system: Literal["veeva_promomats", "aprimo", "package_mode"]
    document_types: list[str]
    required_metadata_fields: list[str]
    allowed_states: list[JobLifecycleState]
    review_route: list[str]
    requires_pharmaceutical_isi: bool
    requires_form_2253: bool
    jurisdiction: str = "US"
    confirmed_by_owner: str


class SourcePackList(list):
    """
    List of source_pack entry dicts that also supports `["entries"]` and `.get("entries")`
    access and assignment so callers using either `list` or `{"entries": [...]}` semantics work seamlessly.
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key == "entries":
                return list(self)
            raise KeyError(key)
        return super().__getitem__(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, str):
            if key == "entries":
                self[:] = list(value)
                return
            raise KeyError(key)
        super().__setitem__(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "entries":
            return list(self)
        return default


class JobRecord(BaseModel):
    """Authoritative job record stored outside conversational memory (Section 2, 3, 8)."""

    # Persistence metadata belongs to the repository, never to public job JSON.
    _storage_revision: int | None = PrivateAttr(default=None)
    _artifact_manifest: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    job_id: str
    tenant_id: str
    title: str
    sector: SectorType
    document_type: str
    destination_platform: Literal["veeva_promomats", "aprimo", "package_mode"]
    deadline_iso: str
    state: JobLifecycleState = JobLifecycleState.DRAFT
    current_version: int = 1
    current_dependency: str = "Genie Pre-Flight & Reference Anchoring"
    dependency_owner: str = "MLR Genie"
    next_action: str = "Review 2 flagged claims and 1 reviewer conflict"
    mandate_allowed_actions: list[str] = Field(
        default_factory=lambda: [
            "retrieve_sources",
            "prepare_annotations",
            "complete_metadata",
            "upload_draft",
            "route_review",
            "resubmit_accepted",
        ]
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    jurisdiction: str = "US"
    audience: str = "HCP & Medical Media"
    channel: str = ""
    product_version: str = "Aurenix-80mg-v1"
    source_pack: list[dict[str, Any]] = Field(default_factory=SourcePackList)
    rd_handoff_boilerplate: str = ""
    reuse_library: list[StatementReuseRecord] = Field(default_factory=list)
    revisions: list[AssetRevisionSnapshot] = Field(default_factory=list)
    claims: list[ClaimAssessment] = Field(default_factory=list)
    comments: list[ReviewComment] = Field(default_factory=list)
    advisory_findings: list[AdvisoryFinding] = Field(default_factory=list)
    receipts: list[ConnectorReceipt] = Field(default_factory=list)
    pv_icrs_flagged: bool = False
    safety_findings: list[VerifiedSafetyFinding] = Field(default_factory=list)
    pv_routing_note: str | None = None
    approved_handoff_bundle_uri: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_source_pack_input(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("source_pack"), dict):
            out = dict(data)
            out["source_pack"] = list(data["source_pack"].get("entries", []))
            return out
        return data

    @model_validator(mode="after")
    def _wrap_source_pack_list(self) -> "JobRecord":
        if not isinstance(self.source_pack, SourcePackList):
            object.__setattr__(self, "source_pack", SourcePackList(self.source_pack))
        return self

