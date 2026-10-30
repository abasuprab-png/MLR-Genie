from copy import deepcopy

import pymupdf
import pytest
import httpx

from evals.live_journal_verification import api_failure_summary, parse_args, verify_pdf_anchor


@pytest.fixture
def anchored_pdf():
    quote = "Journal findings show a consistent signal."
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 100), quote, fontsize=12)
        rect = page.search_for(quote)[0]
        result = {
            "coordinateSpace": "pdf_points",
            "pageDimensions": {"width": page.rect.width, "height": page.rect.height},
            "anchor": {"verdict": "supported", "page": 1, "verbatimQuote": quote,
                       "boundingBox": {"x": rect.x0, "y": rect.y0, "width": rect.width, "height": rect.height}},
        }
        return pdf.tobytes(), result


def test_anchor_is_independently_verified_against_uploaded_pdf(anchored_pdf):
    content, result = anchored_pdf
    assert verify_pdf_anchor(content, result) == {
        "page": 1, "bounding_box_in_page": True, "quote_matches_pdf_rectangle": True, "page_count": 1,
    }


@pytest.mark.parametrize("changed", ["quote", "rectangle", "page", "dimensions", "nan", "verdict"])
def test_false_or_inconsistent_service_anchor_is_refused(anchored_pdf, changed):
    content, original = anchored_pdf
    result = deepcopy(original)
    if changed == "quote":
        result["anchor"]["verbatimQuote"] = "A claim the journal did not make."
    elif changed == "rectangle":
        result["anchor"]["boundingBox"]["x"] = -100
    elif changed == "page":
        result["anchor"]["page"] = 2
    elif changed == "dimensions":
        result["pageDimensions"]["width"] = 12
    elif changed == "nan":
        result["anchor"]["boundingBox"]["width"] = float("nan")
    else:
        result["anchor"]["verdict"] = "insufficient"
    with pytest.raises(ValueError):
        verify_pdf_anchor(content, result)


@pytest.mark.parametrize("base_url", ["http://mlr.example", "https://user:password@mlr.example", "https://mlr.example?token=secret"])
def test_remote_preflight_rejects_unsafe_token_destinations(monkeypatch, base_url):
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "configured-server-secret")
    with pytest.raises(SystemExit) as error:
        parse_args(["--reference-pdf", "source.pdf", "--claim", "A reworded claim.",
                    "--source-url", "https://pubmed.ncbi.nlm.nih.gov/16060722/", "--base-url", base_url])
    assert error.value.code == 2


def test_remote_preflight_requires_existing_secret_without_generating_one(monkeypatch):
    monkeypatch.delenv("MLR_GENIE_BOOTSTRAP_SECRET", raising=False)
    with pytest.raises(SystemExit) as error:
        parse_args(["--reference-pdf", "source.pdf", "--claim", "A reworded claim.",
                    "--source-url", "https://pubmed.ncbi.nlm.nih.gov/16060722/", "--base-url", "https://mlr.example"])
    assert error.value.code == 2
    import os
    assert "MLR_GENIE_BOOTSTRAP_SECRET" not in os.environ


@pytest.mark.parametrize("detail,expected", [
    ("cv08_pv_gate: live Gemini assessment required but unavailable. (provider HTTP 429)",
     "HTTP 502 (live assessment unavailable; provider HTTP 429)"),
    ("cv08_pv_gate: invalid structured Gemini assessment.", "HTTP 502 (structured assessment invalid)"),
    ("Integration verification requires successful live Gemini calls.",
     "HTTP 502 (live claim-verification receipts missing)"),
    ("cv08_pv_gate: cited span is absent from the supplied text.",
     "HTTP 502 (assessment citation absent from supplied text)"),
])
def test_assessment_failure_reports_only_the_controlled_category_and_provider_status(detail, expected):
    response = httpx.Response(502, json={"assessment_failed": True, "detail": detail})
    assert api_failure_summary(response) == expected


def test_failure_summary_does_not_echo_unknown_details_credentials_or_validation_inputs():
    private = "private-document-content Bearer private-session-token"
    for body in (
        {"assessment_failed": True, "detail": private},
        {"detail": [{"input": private}]},
        {"assessment_failed": True, "detail": "live Gemini assessment required but unavailable. " + private},
    ):
        summary = api_failure_summary(httpx.Response(502, json=body))
        assert private not in summary and "private-session-token" not in summary
    assert api_failure_summary(httpx.Response(503, text=private)) == "HTTP 503"
