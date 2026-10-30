from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import zipfile

import docx
from fastapi.testclient import TestClient
import pymupdf
import pytest

from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.document_engine import create_born_digital_reference_pdf
from backend.storage import DurableJobStore


CLAIM = "Signal stability improved by 20% in adults."


@pytest.fixture
def api(monkeypatch, tmp_path):
    import backend.main as main
    monkeypatch.setattr(main, "JOBS_DB", DurableJobStore(tmp_path / "integration.sqlite3"))
    monkeypatch.setattr(main, "router", GeminiModelRouter(GcpAgentConfig(gemini_api_key=None, use_vertex_ai=False)))
    monkeypatch.setattr(main, "_job_artifact_dir", lambda job_id: tmp_path / "artifacts" / job_id)
    monkeypatch.delenv("MLR_REQUIRE_LIVE_GEMINI", raising=False)
    monkeypatch.setenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", "true")
    headers = {"Authorization": "Bearer " + main.issue_signed_session_token("integration-tenant", "human-reviewer")}
    pdf = tmp_path / "reference.pdf"
    create_born_digital_reference_pdf(pdf, pages_spec=[
        {"left_column": ["Study design and methodology."], "right_column": ["Randomized trial design."]},
        {"left_column": [CLAIM], "right_column": ["The report contains the authorized results."]},
    ])
    return main, TestClient(main.app), headers, pdf.read_bytes()


def post(client, headers, pdf, endpoint, payload):
    return client.post(endpoint, headers=headers, data={"payload": json.dumps(payload)},
                       files={"reference_pdf": ("reference.pdf", pdf, "application/pdf")})


def roundtrip_payload():
    return {
        "bodyText": CLAIM, "claims": [{"id": "external-claim", "text": CLAIM}],
        "comments": [{"commentId": "medical", "claimId": "external-claim", "reviewerName": "Reviewer",
                      "reviewerRole": "Medical", "timestamp": "2026-10-30T12:00:00Z",
                      "anchoredPassage": CLAIM, "commentText": "Use increased rather than improved."}],
        "acceptedEdits": [{"commentId": "medical", "edits": [{"targetText": "improved", "replacementText": "increased"}]}],
    }


def test_cams_anchor_contract_and_real_pdf_geometry(api):
    main, client, headers, pdf = api
    response = post(client, headers, pdf, "/integrations/cams/verify-source", {
        "claimText": CLAIM, "source": {"sourceId": "study", "version": "2", "title": "Study"},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"job_id", "claim_id", "anchor", "coordinateSpace", "pageDimensions", "telemetry_receipts"}
    anchor = result["anchor"]
    assert set(anchor) == {"page", "boundingBox", "verdict", "verifiedBy", "verifiedAt", "verbatimQuote"}
    assert anchor["verdict"] == "supported" and anchor["page"] == 2
    assert anchor["verifiedBy"] == "mlr-genie:offline+pymupdf"
    assert datetime.fromisoformat(anchor["verifiedAt"].replace("Z", "+00:00")).tzinfo is not None
    assert result["coordinateSpace"] == "pdf_points"
    box = anchor["boundingBox"]
    assert set(box) == {"x", "y", "width", "height"}
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[anchor["page"] - 1]
        rect = pymupdf.Rect(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
        assert rect in page.rect
        assert " ".join(page.get_text(clip=rect + (-1, -1, 1, 1)).split()) == " ".join(anchor["verbatimQuote"].split())
    saved = main.JOBS_DB.get_for_tenant(result["job_id"], "integration-tenant")
    assert saved.claims[0].claim_id == result["claim_id"]
    assert saved.metadata["integration_source"]["sourceId"] == "study"
    assert not result["telemetry_receipts"][0]["used_live_sdk"]


@pytest.mark.parametrize("claim,expected", [(CLAIM.replace("20%", "80%"), "contradicted"),
                                           (CLAIM + " Novel mechanism.", "insufficient")])
def test_cams_verdict_mapping_and_one_claim_id_for_multiple_sentences(api, claim, expected):
    main, client, headers, pdf = api
    response = post(client, headers, pdf, "/integrations/cams/verify-source", {"claimText": claim})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["anchor"]["verdict"] == expected
    assert len(main.JOBS_DB[result["job_id"]].claims) == 1
    assert main.JOBS_DB[result["job_id"]].claims[0].sentence_text == claim


def test_cams_textless_pdf_has_no_fabricated_anchor(api):
    _, client, headers, _ = api
    with pymupdf.open() as document:
        document.new_page()
        pdf = document.tobytes()
    response = post(client, headers, pdf, "/integrations/cams/verify-source", {"claimText": CLAIM})
    assert response.status_code == 200, response.text
    anchor = response.json()["anchor"]
    assert anchor["verdict"] == "insufficient"
    assert "page" not in anchor and "boundingBox" not in anchor and "verbatimQuote" not in anchor


@pytest.mark.parametrize("endpoint,payload", [
    ("/integrations/cams/verify-source", {"claimText": CLAIM}),
    ("/integrations/commsrx/roundtrip", roundtrip_payload()),
])
def test_integrations_auth_pdf_validation_and_required_live_errors(api, endpoint, payload):
    _, client, headers, pdf = api
    assert post(client, {}, pdf, endpoint, payload).status_code == 401
    assert post(client, headers, b"not a pdf", endpoint, payload).status_code == 400
    assert post(client, headers, b"", endpoint, payload).status_code == 400
    assert post(client, headers, pdf, endpoint, {**payload, "requireLive": True}).status_code == 502
    assert post(client, headers, pdf, endpoint, {**payload, "tenantId": "spoofed"}).status_code == 422


def test_commsrx_roundtrip_contract_real_files_and_authenticated_downloads(api):
    main, client, headers, pdf = api
    response = post(client, headers, pdf, "/integrations/commsrx/roundtrip", roundtrip_payload())
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"job_id", "state", "requires_human_resolution", "claims", "artifacts", "telemetry_receipts"}
    assert not result["requires_human_resolution"]
    artifacts = result["artifacts"]
    assert set(artifacts) == {"redline.docx", "clean_revised.docx", "audit_summary.pdf", "annotated_source.pdf"}
    files = {}
    other_tenant = {"Authorization": "Bearer " + main.issue_signed_session_token("other-tenant", "reviewer")}
    for filename, descriptor in artifacts.items():
        assert set(descriptor) == {"filename", "download_url", "media_type", "bytes", "sha256"}
        assert descriptor["filename"] == filename
        assert client.get(descriptor["download_url"]).status_code == 401
        assert client.get(descriptor["download_url"], headers=other_tenant).status_code == 403
        download = client.get(descriptor["download_url"], headers=headers)
        assert download.status_code == 200
        assert filename in download.headers["content-disposition"]
        assert download.headers["content-type"] == descriptor["media_type"]
        assert len(download.content) == descriptor["bytes"]
        assert hashlib.sha256(download.content).hexdigest() == descriptor["sha256"]
        files[filename] = download.content
    clean = docx.Document(io.BytesIO(files["clean_revised.docx"]))
    assert clean.paragraphs[0].text == CLAIM.replace("improved", "increased")
    with zipfile.ZipFile(io.BytesIO(files["redline.docx"])) as archive:
        xml = archive.read("word/document.xml").decode()
        assert "<w:del " in xml and "<w:ins " in xml
    with pymupdf.open(stream=files["audit_summary.pdf"], filetype="pdf") as audit:
        text = "".join(page.get_text() for page in audit)
        assert "MLR Genie review audit" in text and "accepted" in text and "human-reviewer" in text
    with pymupdf.open(stream=files["annotated_source.pdf"], filetype="pdf") as annotated:
        assert len(annotated) == 2
        assert any(list(page.annots() or []) for page in annotated)
    assert files["audit_summary.pdf"] != files["annotated_source.pdf"]
    assert main.JOBS_DB[result["job_id"]].metadata["commsrx_roundtrip"] == result


@pytest.mark.parametrize("invalid", ["claim", "comment_claim", "accepted_comment", "duplicate"])
def test_commsrx_rejects_invalid_claim_and_edit_references(api, invalid):
    _, client, headers, pdf = api
    payload = roundtrip_payload()
    if invalid == "claim":
        payload["claims"][0]["text"] = "Not in the copy"
    elif invalid == "comment_claim":
        payload["comments"][0]["claimId"] = "missing"
    elif invalid == "accepted_comment":
        payload["acceptedEdits"][0]["commentId"] = "missing"
    else:
        payload["claims"] *= 2
    assert post(client, headers, pdf, "/integrations/commsrx/roundtrip", payload).status_code == 422


def test_commsrx_does_not_return_success_for_unplaced_edits_or_unresolved_conflicts(api):
    _, client, headers, pdf = api
    payload = roundtrip_payload()
    payload["acceptedEdits"][0]["edits"][0]["targetText"] = "not present"
    result = post(client, headers, pdf, "/integrations/commsrx/roundtrip", payload)
    assert result.status_code == 409 and result.json()["detail"]["unplaced_comment_ids"] == ["medical"]
    payload = roundtrip_payload()
    payload["comments"][0]["proposedEdits"] = payload.pop("acceptedEdits")[0]["edits"]
    payload["comments"].append({**payload["comments"][0], "commentId": "regulatory", "reviewerRole": "Regulatory",
                                "proposedEdits": [{"targetText": "improved", "replacementText": "decreased"}]})
    result = post(client, headers, pdf, "/integrations/commsrx/roundtrip", payload)
    assert result.status_code == 409 and result.json()["detail"]["requires_human_resolution"] is True
