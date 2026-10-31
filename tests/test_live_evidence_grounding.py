"""Live verdicts must cite the supplied passage they actually verified."""
import json

import pymupdf
import pytest

from backend.agent.models_router import GeminiModelRouter, ModelCallTelemetry
from backend.config import GcpAgentConfig
from backend.document_engine import inspect_pdf_document_and_extract_source_pack
from backend.domain.schemas import JobRecord, SectorType, SupportClassification
from backend.tools.document_tools import _source_passage_id, ingest_working_document, locate_and_anchor_evidence


CLAIM = "Larger samples improve statistical power when other factors are equal."
PASSAGE = "When other factors are equal, larger samples provide greater statistical power."


class LiveRouter(GeminiModelRouter):
    def __init__(self, payload):
        super().__init__(GcpAgentConfig(gemini_api_key=None, use_vertex_ai=False))
        self._client = object()
        self.payload, self.calls = payload, []

    def create_interaction(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload, ModelCallTelemetry(
            model="gemini-4-argon", task_type="claim_verification",
            interaction_id="grounding-test", used_live_sdk=True, transport_mode="interactions",
        )


@pytest.fixture
def source_pack(tmp_path):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 100), "Definition: statistical power.")
        pdf.new_page().insert_text((72, 100), PASSAGE, fontsize=10)
        pdf.save(path)
    sources = inspect_pdf_document_and_extract_source_pack(path)["source_pack"]
    # Make the lexical suggestion favor the definition; semantic selection must win.
    sources[0]["keywords"] = ["larger", "samples", "statistical", "power"] * 4
    return path, sources


def assess(sources, payload, claim=CLAIM):
    job = JobRecord(job_id="grounding", tenant_id="test", title="Evidence grounding",
                    sector=SectorType.CORPORATE, document_type="Brief", destination_platform="package_mode",
                    deadline_iso="2026-12-01T00:00:00Z")
    ingest_working_document(job=job, body_text=claim, actor="test")
    router = LiveRouter(payload)
    result = locate_and_anchor_evidence(job=job, source_pack=sources, router=router, claim_texts=[claim])[0]
    assert len(router.calls) == 1
    return result, router.calls[0]


def test_live_selected_passage_replaces_unrelated_lexical_anchor_using_real_pdf_geometry(source_pack):
    path, sources = source_pack
    result, call = assess(sources, {"status": "supported", "verbatim_quote": PASSAGE,
                                   "page_number": 2, "box_2d": [0, 0, 1000, 1000]})
    assert json.loads(call["prompt"])["matched_source"]["bbox"]["page"] == 1
    assert result.support_status == SupportClassification.SUPPORTED
    assert len(result.anchors) == 1
    anchor = result.anchors[0]
    assert anchor.page_number == 2 and anchor.verbatim_quote == PASSAGE
    with pymupdf.open(path) as pdf:
        rect = pymupdf.Rect(anchor.x0, anchor.y0, anchor.x1, anchor.y1)
        assert " ".join(pdf[1].get_text(clip=rect + (-1, -1, 1, 1)).split()) == PASSAGE
        assert rect != pdf[1].rect  # Ignore generated coordinates; use extracted source geometry.


@pytest.mark.parametrize("quote,page", [("Invented evidence.", 2), (PASSAGE, 1), ("", 2)])
def test_ungrounded_live_support_is_unresolved_with_no_anchor(source_pack, quote, page):
    _, sources = source_pack
    result, _ = assess(sources, {"status": "supported", "verbatim_quote": quote, "page_number": page})
    assert result.support_status == SupportClassification.UNRESOLVED
    assert result.anchors == []


def test_ambiguous_duplicate_passages_do_not_use_generated_coordinates_to_choose_an_anchor(source_pack):
    _, sources = source_pack
    sources.append(dict(sources[1]))
    result, _ = assess(sources, {"status": "supported", "verbatim_quote": PASSAGE, "page_number": 2})
    assert result.support_status == SupportClassification.UNRESOLVED and result.anchors == []


def test_hard_numeric_checks_use_the_model_selected_source_before_accepting_support(source_pack):
    _, sources = source_pack
    sources[1]["verbatim_text"] = "Signal reduced risk by 30%."
    result, _ = assess(sources, {"status": "supported", "verbatim_quote": sources[1]["verbatim_text"],
                                "page_number": 2}, claim="Signal reduced risk by 20%.")
    assert result.support_status == SupportClassification.CONTRADICTED
    assert result.anchors[0].page_number == 2


def test_identifier_selects_original_pdf_text_without_relying_on_model_transcription(source_pack):
    path, sources = source_pack
    result, call = assess(sources, {
        "status": "supported", "source_passage_id": _source_passage_id(sources[1]),
        "verbatim_quote": "Model-normalized text is never used as the citation.", "page_number": 2,
    })
    assert result.support_status == SupportClassification.SUPPORTED
    anchor = result.anchors[0]
    assert anchor.verbatim_quote == PASSAGE and anchor.page_number == 2
    supplied = json.loads(call["prompt"])["authorized_source_passages"]
    assert supplied[1]["passage_id"] == _source_passage_id(sources[1])
    assert all("passage_id" not in item for item in sources)  # Do not mutate stored source data.
    with pymupdf.open(path) as pdf:
        assert " ".join(pdf[1].get_text(clip=pymupdf.Rect(anchor.x0, anchor.y0, anchor.x1, anchor.y1) + (-1, -1, 1, 1)).split()) == PASSAGE


@pytest.mark.parametrize("kind", ["invented", "wrong_page", "changed_source", "duplicate"])
def test_invalid_passage_identifier_cannot_fall_back_to_a_valid_quote(source_pack, kind):
    _, sources = source_pack
    identifier = _source_passage_id(sources[1])
    page = 2
    if kind == "invented":
        identifier = "passage_invented"
    elif kind == "wrong_page":
        page = 1
    elif kind == "changed_source":
        sources[1]["version"] = "changed"
    else:
        sources.append(dict(sources[1]))
    result, _ = assess(sources, {"status": "supported", "source_passage_id": identifier,
                               "verbatim_quote": PASSAGE, "page_number": page})
    assert result.support_status == SupportClassification.UNRESOLVED and result.anchors == []


def test_identifier_selection_retains_hard_numeric_rejection(source_pack):
    _, sources = source_pack
    sources[1]["verbatim_text"] = "Signal reduced risk by 30%."
    result, _ = assess(sources, {
        "status": "supported", "source_passage_id": _source_passage_id(sources[1]), "page_number": 2,
    }, claim="Signal reduced risk by 20%.")
    assert result.support_status == SupportClassification.CONTRADICTED
