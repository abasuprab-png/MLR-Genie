from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google import genai

from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.domain.schemas import JobRecord, SectorType, SupportClassification
from backend.storage import DurableJobStore
from backend.tools.document_tools import ingest_working_document, locate_and_anchor_evidence
from backend.tools.review_tools import analyze_approval_day_label_delta


def make_job(body: str = "Signal texture balance.") -> JobRecord:
    job = JobRecord(
        job_id="job_v34",
        tenant_id="tenant_v34",
        title="Hardening regression",
        sector=SectorType.MEDTECH,
        document_type="Brief",
        destination_platform="package_mode",
        deadline_iso="2026-12-01T00:00:00Z",
    )
    ingest_working_document(job=job, body_text=body, actor="reviewer_v34")
    return job


@pytest.fixture
def offline_router():
    return GeminiModelRouter(GcpAgentConfig(gemini_api_key=None, use_vertex_ai=False))


def assess(claim, source, router):
    return locate_and_anchor_evidence(
        job=make_job(claim),
        source_pack=[{"source_id": "source_v34", "keywords": ["signal"], **source}],
        router=router,
    )[0]


@pytest.mark.parametrize(
    "claim,expected,missing",
    [
        ("Signal texture novel mechanism.", SupportClassification.PARTIALLY_SUPPORTED, "mechanism, novel"),
        ("Signal texture balance novelty.", SupportClassification.SUPPORTED, None),  # Exactly 0.75
        ("SIGNAL signal texture balance novelty.", SupportClassification.SUPPORTED, None),
        ("Signal texture balance evaluated treated received administered.", SupportClassification.SUPPORTED, None),
    ],
)
def test_offline_content_coverage_threshold_and_generic_verbs(offline_router, claim, expected, missing):
    claim_result = assess(claim, {"verbatim_text": "Signal texture balance."}, offline_router)
    assert claim_result.support_status == expected
    if missing:
        assert claim_result.plain_english_diagnosis == (
            f"Needs closer review: Claim introduces unverified terms ({missing}) "
            "not established in the matched source passage."
        )


@pytest.mark.parametrize(
    "field", ["keywords", "required_qualifiers", "population", "endpoint", "comparator", "direction", "magnitude", "timeframe"]
)
def test_offline_coverage_includes_matched_source_metadata(offline_router, field):
    value = ["balance novelty"] if field in ("keywords", "required_qualifiers") else "balance novelty"
    result = assess(
        "Signal texture balance novelty.",
        {"verbatim_text": "Signal texture.", field: value},
        offline_router,
    )
    assert result.support_status == SupportClassification.SUPPORTED


def test_offline_coverage_handles_no_content_tokens(offline_router):
    result = assess("The study evaluated patients.", {"verbatim_text": "The study treated patients.", "keywords": ["study"]}, offline_router)
    assert result.support_status == SupportClassification.SUPPORTED


@pytest.mark.parametrize(
    "claim,source,expected,diagnosis",
    [
        ("Signal reduced risk by 50% with novel mechanism.", {"verbatim_text": "Signal reduced risk by 20%."}, SupportClassification.CONTRADICTED, "numeric"),
        ("Signal novel mechanism.", {"verbatim_text": "Signal texture in adults.", "required_qualifiers": ["adults"]}, SupportClassification.PARTIALLY_SUPPORTED, "Missing required qualifier"),
        ("Signal novel mechanism.", {"verbatim_text": ""}, SupportClassification.UNRESOLVED, "empty"),
    ],
)
def test_coverage_preserves_existing_failure_precedence(offline_router, claim, source, expected, diagnosis):
    result = assess(claim, source, offline_router)
    assert result.support_status == expected
    assert diagnosis in result.plain_english_diagnosis


def test_coverage_does_not_borrow_terms_from_an_unmatched_source(offline_router):
    result = locate_and_anchor_evidence(
        job=make_job("Signal novel mechanism benefit."),
        source_pack=[
            {"source_id": "matched", "keywords": ["signal"], "verbatim_text": "Signal texture."},
            {"source_id": "unmatched", "keywords": ["absent"], "verbatim_text": "Novel mechanism benefit."},
        ],
        router=offline_router,
    )[0]
    assert result.support_status == SupportClassification.PARTIALLY_SUPPORTED
    assert result.anchors[0].source_document_id == "matched"


def test_live_verdict_is_not_downgraded_by_offline_token_coverage(offline_router):
    offline_router._client = MagicMock()
    offline_router._client.models.generate_content.return_value = SimpleNamespace(
        text='{"status": "supported", "verbatim_quote": "Signal texture.", "page_number": 1}', id="verified_live"
    )
    result = assess("Signal novel mechanism benefit.", {"verbatim_text": "Signal texture.", "bbox": {"page": 1}}, offline_router)
    assert result.support_status == SupportClassification.SUPPORTED
    assert offline_router.telemetry_log[-1].used_live_sdk is True


@pytest.fixture
def api(monkeypatch, tmp_path, offline_router):
    import backend.main as main

    monkeypatch.setenv("MLR_GENIE_ENV", "local")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("MLR_GENIE_SESSION_SECRET", "v34-local-test-signing-secret")
    monkeypatch.setattr(main, "JOBS_DB", DurableJobStore(tmp_path / "api.sqlite3"))
    monkeypatch.setattr(main, "router", offline_router)
    main.JOBS_DB.save_job(make_job("The agent binds a receptor. Response was 20%."))
    with TestClient(main.app) as client:
        yield main, client


def token_headers(client, tenant_id="tenant_v34", **overrides):
    response = client.post("/auth/token", json={"tenant_id": tenant_id, "actor_id": "reviewer_v34", **overrides})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_token_endpoint_signs_normalized_identity_role_and_expiry(api, monkeypatch):
    main, client = api
    monkeypatch.setattr(main.time, "time", lambda: 1_800_000_000)
    response = client.post("/auth/token", json={
        "tenant_id": " tenant_v34 ", "actor_id": " reviewer_v34 ", "role": " approver ", "ttl_seconds": 120,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == 120
    assert main.verify_signed_session_token(payload["access_token"]) == {
        "tenant_id": "tenant_v34", "actor_id": "reviewer_v34", "role": "approver",
        "iat": 1_800_000_000, "exp": 1_800_000_120,
    }
    default = client.post("/auth/token", json={"tenant_id": "tenant_v34", "actor_id": "reviewer_v34"}).json()
    assert default["role"] == "requester"
    assert default["expires_in"] == 3600


@pytest.mark.parametrize("override,status", [({"tenant_id": "  "}, 400), ({"actor_id": ""}, 400), ({"ttl_seconds": 0}, 422), ({"ttl_seconds": -1}, 422)])
def test_token_endpoint_rejects_invalid_identity_or_ttl(api, override, status):
    _, client = api
    response = client.post("/auth/token", json={"tenant_id": "tenant_v34", "actor_id": "reviewer_v34", **override})
    assert response.status_code == status


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE"])
def test_strict_bearer_rejects_legacy_and_query_auth(api, monkeypatch, flag):
    _, client = api
    legacy = {"X-Tenant-Id": "tenant_v34", "X-Actor-Id": "reviewer_v34"}
    assert client.get("/jobs/job_v34", headers=legacy).status_code == 200
    monkeypatch.setenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", flag)
    assert client.get("/jobs/job_v34", headers=legacy).status_code == 401
    assert client.get("/jobs/job_v34").status_code == 401
    assert client.get(
        "/jobs/job_v34/artifacts/01_working_draft_v1.docx",
        params={"tenant_id": "tenant_v34", "actor_id": "reviewer_v34"},
    ).status_code == 401
    assert client.get("/jobs/job_v34", headers=token_headers(client)).status_code == 200


def test_strict_bearer_preserves_signature_expiry_and_tenant_checks(api, monkeypatch):
    main, client = api
    monkeypatch.setenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", "true")
    headers = token_headers(client)
    signed = headers["Authorization"]
    tampered = signed[:-1] + ("a" if signed[-1] != "a" else "b")
    with monkeypatch.context() as clock:
        clock.setattr(main.time, "time", lambda: 1_000_000_000)
        expired = main.issue_signed_session_token("tenant_v34", "reviewer_v34", ttl_seconds=1)
    for auth in ["Basic invalid", "Bearer invalid", f"Bearer {signed}", tampered, f"Bearer {expired}"]:
        assert client.get("/jobs/job_v34", headers={"Authorization": auth}).status_code == 401
    assert client.get("/jobs/job_v34", headers=token_headers(client, tenant_id="other_tenant")).status_code == 403
    assert client.get("/jobs/job_v34", headers={**headers, "X-Actor-Id": "other_actor"}).status_code == 403


def test_sqlite_initialization_falls_back_and_persists_jobs(monkeypatch, tmp_path, caplog):
    import backend.storage as storage

    original = tmp_path / "unsupported" / "jobs.sqlite3"
    fallback_dir = tmp_path / "temporary"
    init_db = DurableJobStore._init_db

    def initialize(store):
        if store.db_path == original:
            raise sqlite3.OperationalError("disk I/O error")
        init_db(store)

    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(fallback_dir))
    monkeypatch.setattr(DurableJobStore, "_init_db", initialize)
    store = DurableJobStore(original)
    assert store.db_path == fallback_dir / original.name
    store.save_job(make_job())
    reopened = DurableJobStore(original)
    assert reopened.get_for_tenant("job_v34", "tenant_v34").revisions[-1].body == "Signal texture balance."
    assert "temporary storage" in caplog.text


def test_sqlite_fallback_failure_is_not_swallowed(monkeypatch, tmp_path):
    import backend.storage as storage

    monkeypatch.setattr(storage.tempfile, "gettempdir", lambda: str(tmp_path / "fallback"))
    attempts = []

    def fail(store):
        attempts.append(store.db_path)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(DurableJobStore, "_init_db", fail)
    with pytest.raises(sqlite3.OperationalError):
        DurableJobStore(tmp_path / "original" / "jobs.sqlite3")
    assert attempts == [tmp_path / "original" / "jobs.sqlite3", tmp_path / "fallback" / "jobs.sqlite3"]


def test_label_diff_flags_only_missing_moa_and_retained_old_figures(offline_router):
    moa = "The agent binds a receptor."
    result = analyze_approval_day_label_delta(
        prepared_scenarios=[{"scenario_id": "before"}],
        final_approved_label={"section_12_1_moa": moa, "section_14_clinical_figures": [{"endpoint": "response", "old_value": "20%", "new_value": "25%"}]},
        launch_kit_assets=[
            {"asset_id": "missing_moa", "body": "Response was 25%."},
            {"asset_id": "stale", "body": moa + " Response was 20%."},
            {"asset_id": "updated", "body": moa + " Response was 25%."},
            {"asset_id": "no_figure", "body": "THE AGENT\nBINDS A RECEPTOR."},
            {"asset_id": "different_number", "body": moa + " Signal was 120%."},
        ],
        router=offline_router,
    )
    affected = {asset["asset_id"]: asset for asset in result["affected_assets"]}
    assert set(affected) == {"missing_moa", "stale"}
    assert "Section 12.1" in affected["missing_moa"]["impact_reasons"][0]
    assert "Section 14 update (response): replace '20%' with '25%'" in affected["stale"]["impact_reasons"]
    assert result["fits_prepared_scenario"] is False
    assert result["new_review_path_required"] is True


@pytest.mark.parametrize(
    "old,new,body,affected",
    [(0, 1, "Value 0.", True), ("5", "7", "Value 0.5.", False), ("20%", "20%", "Value 20%.", False), ("p<0.05", "p<0.01", "Value p<0x05.", False)],
)
def test_section_14_value_boundaries_zero_and_unchanged_values(offline_router, old, new, body, affected):
    result = analyze_approval_day_label_delta(
        prepared_scenarios=[],
        final_approved_label={"section_14_clinical_figures": [{"endpoint": "metric", "old_value": old, "new_value": new}]},
        launch_kit_assets=[{"asset_id": "asset", "body": body}],
        router=offline_router,
    )
    assert bool(result["affected_assets"]) is affected


def test_label_diff_can_match_a_prepared_scenario_with_new_sections(offline_router):
    label = {
        "section_12_1_moa": "The agent binds a receptor.",
        "section_14_clinical_figures": [{"endpoint": "response", "old_value": "20%", "new_value": "25%"}],
    }
    result = analyze_approval_day_label_delta(
        prepared_scenarios=[{"scenario_id": "reviewed", **label}],
        final_approved_label=label,
        launch_kit_assets=[{"asset_id": "updated", "body": "The agent binds a receptor. Response was 25%."}],
        router=offline_router,
    )
    assert result["matched_scenario_id"] == "reviewed"
    assert result["new_review_path_required"] is False


def test_diff_label_route_authenticates_and_persists_latest_revision_analysis(api, monkeypatch):
    main, client = api
    monkeypatch.setenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", "true")
    payload = {"final_approved_label": {"section_14_clinical_figures": [{"endpoint": "response", "old_value": "20%", "new_value": "25%"}]}}
    assert client.post("/jobs/job_v34/diff-label", json=payload).status_code == 401
    assert client.post("/jobs/missing/diff-label", json=payload).status_code == 401
    assert client.post("/jobs/job_v34/diff-label", headers=token_headers(client, "other_tenant"), json=payload).status_code == 403
    assert "last_approval_day_label_delta" not in main.JOBS_DB["job_v34"].metadata
    headers = token_headers(client)
    assert client.post("/jobs/missing/diff-label", headers=headers, json=payload).status_code == 404
    response = client.post("/jobs/job_v34/diff-label", headers=headers, json=payload)
    assert response.status_code == 200
    delta = response.json()["label_delta"]
    assert delta["affected_assets"][0]["asset_id"] == "job_v34"
    stored = DurableJobStore(main.JOBS_DB.db_path)["job_v34"]
    assert stored.metadata["last_approval_day_label_delta"] == delta
    assert stored.revisions[-1].body == "The agent binds a receptor. Response was 20%."
    assert main.genie.registered_tools["analyze_approval_day_label_delta"] is analyze_approval_day_label_delta
    response = client.post("/jobs/job_v34/diff-label", headers=headers, json={
        **payload, "launch_kit_assets": [{"asset_id": "supplied", "body": "Response was 25%."}],
    })
    assert response.status_code == 200
    assert response.json()["label_delta"]["affected_assets"] == []


@pytest.fixture(params=["sdk", "mock"])
def sdk_router(request, offline_router):
    # Use the actual SDK class, but mock every transport invoked by these tests.
    client = genai.Client(api_key="unit-test-only", vertexai=False) if request.param == "sdk" else MagicMock()
    offline_router._client = client
    yield offline_router, request.param == "sdk"
    if request.param == "sdk":
        client.close()


@pytest.mark.parametrize("logical,resolved", [
    ("gemini-4-argon", "gemini-4-argon"),
    ("gemini-3.8-flash", "gemini-3.8-flash"),
    ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
    ("gemini-embedding-2", "gemini-embedding-001"),
    ("custom-model", "custom-model"),
])
def test_model_resolution_only_changes_sdk_clients(sdk_router, logical, resolved):
    router, is_sdk = sdk_router
    assert router.resolve_live_sdk_model_id(logical) == (resolved if is_sdk else logical)
    assert GeminiModelRouter(GcpAgentConfig(gemini_api_key=None, use_vertex_ai=False)).resolve_live_sdk_model_id(logical) == logical


@pytest.mark.parametrize("transport", ["generate_content", "interactions"])
@pytest.mark.parametrize("retry", [False, True])
def test_sdk_model_resolution_on_both_transports_and_cache_retry(sdk_router, monkeypatch, transport, retry):
    router, is_sdk = sdk_router
    response = SimpleNamespace(id="sdk_response", text='{"status":"supported"}', output_text='{"status":"supported"}')
    call = MagicMock(side_effect=[RuntimeError("expired cache"), response] if retry else [response])
    monkeypatch.setattr(router, "_should_use_generate_content", lambda: transport == "generate_content")
    if transport == "generate_content":
        monkeypatch.setattr(router._client.models, "generate_content", call)
    else:
        monkeypatch.setattr(router._client.interactions, "create", call)
    result, telemetry = router.create_interaction(
        task_type="claim_verification", prompt="Verify claim", system_instruction="Verify evidence",
        response_schema={"type": "object"}, cached_content="cachedContents/reference",
    )
    assert result == {"status": "supported"}
    assert call.call_count == (2 if retry else 1)
    assert all(c.kwargs["model"] == "gemini-4-argon" for c in call.call_args_list)
    assert telemetry.model == "gemini-4-argon"
    assert telemetry.effective_model == "gemini-4-argon"
    assert telemetry.used_live_sdk is True
    assert telemetry.transport_mode == transport
    assert telemetry.cached_content == (None if retry else "cachedContents/reference")
    if is_sdk and transport == "generate_content":
        first = call.call_args_list[0].kwargs
        assert "system_instruction" not in first["config"]
        assert first["contents"] == "Verify evidence\n\nVerify claim"
    if retry:
        last = call.call_args.kwargs
        assert "cached_content" not in (last["config"] if transport == "generate_content" else last)
        if transport == "generate_content":
            assert last["config"]["system_instruction"] == "Verify evidence"


def test_sdk_cache_and_embedding_resolve_ids_without_changing_logical_metadata(sdk_router, monkeypatch):
    router, is_sdk = sdk_router
    create_cache = MagicMock(return_value=SimpleNamespace(name="cachedContents/reference"))
    embed = MagicMock(return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0])]))
    monkeypatch.setattr(router._client.caches, "create", create_cache)
    monkeypatch.setattr(router._client.models, "embed_content", embed)
    cache = router.create_reference_context_cache(cache_key="reference", pdf_or_text_content="Source passage")
    assert create_cache.call_args.kwargs["model"] == "gemini-4-argon"
    assert cache["model"] == "gemini-4-argon"
    assert cache["effective_model"] == "gemini-4-argon"
    assert router.create_reference_context_cache(cache_key="reference", pdf_or_text_content="Source passage") == cache
    assert create_cache.call_count == 1
    assert router.embed_multimodal("Source passage", dimensions=2) == pytest.approx([0.6, 0.8])
    assert embed.call_args.kwargs["model"] == ("gemini-embedding-001" if is_sdk else "gemini-embedding-2")
