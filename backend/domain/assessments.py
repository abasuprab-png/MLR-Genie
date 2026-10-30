"""Structured model proposals; application code verifies their citations before use."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedClaim(StrictAssessment):
    span: str = Field(min_length=1)
    rationale: str


class SafetyFinding(StrictAssessment):
    kind: Literal["individual_adverse_event", "safety_statement", "training_example"]
    span: str = Field(min_length=1)
    rationale: str
    patient_span: str
    product_span: str
    event_span: str


class IntakeAssessment(StrictAssessment):
    claims: list[ExtractedClaim]
    safety_findings: list[SafetyFinding]


class VerifiedSafetyFinding(SafetyFinding):
    start: int
    end: int
    source_sha256: str
    used_live_sdk: bool


class LabelClaimImpact(StrictAssessment):
    section: str
    asset_id: str
    claim_quote: str = Field(min_length=1)
    label_quote: str = Field(min_length=1)
    impact: Literal["none", "revise", "human_review"]
    rationale: str
    proposed_wording: str


class LabelAssessment(StrictAssessment):
    fits_prepared_scenario: bool
    rationale: str
    impacts: list[LabelClaimImpact]


class CommentConflict(StrictAssessment):
    comment_ids: list[str] = Field(min_length=2, max_length=2)
    rationale: str


class CommentAssessment(StrictAssessment):
    conflicts: list[CommentConflict]
