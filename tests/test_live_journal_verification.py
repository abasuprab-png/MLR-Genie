from copy import deepcopy

import pymupdf
import pytest

from evals.live_journal_verification import parse_args, verify_pdf_anchor


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
