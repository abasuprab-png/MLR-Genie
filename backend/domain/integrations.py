"""Public Stage 3/4 wire contracts. Coordinate units are PDF points, never CSS pixels."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from backend.domain.schemas import SectorType


class IntegrationInput(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", populate_by_name=True)


class SourceMetadata(IntegrationInput):
    source_id: str = "reference"
    version: str = "1"
    title: str | None = None
    url: str | None = None


class CamsVerificationInput(IntegrationInput):
    claim_text: str = Field(min_length=1)
    source: SourceMetadata = Field(default_factory=SourceMetadata)
    require_live: bool = False


class InputClaim(IntegrationInput):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SpanEdit(IntegrationInput):
    target_text: str = Field(min_length=1)
    replacement_text: str


class IntegrationComment(IntegrationInput):
    comment_id: str = Field(min_length=1)
    claim_id: str | None = None
    reviewer_name: str = Field(min_length=1)
    reviewer_role: Literal["Medical", "Legal", "Regulatory", "MLR Operations"]
    timestamp: datetime
    anchored_passage: str = Field(min_length=1)
    comment_text: str = Field(min_length=1)
    proposed_edits: list[SpanEdit] = Field(default_factory=list)
    decision: Literal["pending", "accepted", "rejected"] = "pending"


class AcceptedEdit(IntegrationInput):
    comment_id: str = Field(min_length=1)
    edits: list[SpanEdit] = Field(min_length=1)


class CommsRxRoundtripInput(IntegrationInput):
    title: str = "CommsRx review"
    body_text: str = Field(min_length=1)
    claims: list[InputClaim] = Field(min_length=1)
    comments: list[IntegrationComment] = Field(default_factory=list)
    accepted_edits: list[AcceptedEdit] = Field(default_factory=list)
    source: SourceMetadata = Field(default_factory=SourceMetadata)
    sector: SectorType = SectorType.CORPORATE
    require_live: bool = False

    @model_validator(mode="after")
    def validate_targets(self):
        claim_ids = {c.id for c in self.claims}
        comment_ids = {c.comment_id for c in self.comments}
        if len(claim_ids) != len(self.claims) or len(comment_ids) != len(self.comments):
            raise ValueError("Claim and comment IDs must be unique.")
        if len({e.comment_id for e in self.accepted_edits}) != len(self.accepted_edits):
            raise ValueError("Each comment may have only one accepted edit instruction.")
        if any(not c.text.strip() or c.text not in self.body_text for c in self.claims):
            raise ValueError("Each claim must appear verbatim in bodyText.")
        for c in self.comments:
            if c.claim_id is not None and c.claim_id not in claim_ids:
                raise ValueError("Comment references an unknown claimId.")
            if not c.anchored_passage.strip() or c.anchored_passage not in self.body_text:
                raise ValueError("Comment passage must appear verbatim in bodyText.")
            if c.claim_id is not None:
                target = next(claim for claim in self.claims if claim.id == c.claim_id)
                if c.anchored_passage not in target.text and target.text not in c.anchored_passage:
                    raise ValueError("Comment passage does not match its claimId.")
        if any(edit.comment_id not in comment_ids for edit in self.accepted_edits):
            raise ValueError("Accepted edit references an unknown commentId.")
        return self


class CamsBoundingBox(BaseModel):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    width: float = Field(gt=0, allow_inf_nan=False)
    height: float = Field(gt=0, allow_inf_nan=False)


class CamsSourceAnchor(BaseModel):
    page: int | None = Field(default=None, ge=1)
    boundingBox: CamsBoundingBox | None = None
    verdict: Literal["supported", "contradicted", "insufficient"]
    verifiedBy: str
    verifiedAt: datetime
    verbatimQuote: str | None = None


class PageDimensions(BaseModel):
    width: float
    height: float


class CamsVerificationResponse(BaseModel):
    job_id: str
    claim_id: str
    anchor: CamsSourceAnchor
    coordinateSpace: Literal["pdf_points"] = "pdf_points"
    pageDimensions: PageDimensions | None = None
    telemetry_receipts: list[dict]


class IntegrationArtifact(BaseModel):
    filename: str
    download_url: str
    media_type: str
    bytes: int = Field(gt=0)
    sha256: str


class CommsRxArtifacts(BaseModel):
    redline: IntegrationArtifact = Field(alias="redline.docx")
    clean: IntegrationArtifact = Field(alias="clean_revised.docx")
    audit: IntegrationArtifact = Field(alias="audit_summary.pdf")
    source: IntegrationArtifact = Field(alias="annotated_source.pdf")


class IntegrationClaimResult(BaseModel):
    claim_id: str
    text: str
    verdict: Literal["supported", "contradicted", "insufficient"]


class CommsRxRoundtripResponse(BaseModel):
    job_id: str
    state: str
    requires_human_resolution: Literal[False] = False
    claims: list[IntegrationClaimResult]
    artifacts: CommsRxArtifacts
    telemetry_receipts: list[dict]
