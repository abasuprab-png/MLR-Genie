"""Exercise installed SDK validation and serialization without remote requests."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.oauth2.credentials import Credentials

from backend.agent.gemini_gateway import GeminiGateway, ModelAssessmentError
from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.domain.assessments import IntakeAssessment


def sdk_router(handler, *, vertex=True):
    config = GcpAgentConfig(gemini_api_key=None, use_vertex_ai=False)
    router = GeminiModelRouter(config)
    options = {
        "client_args": {"transport": httpx.MockTransport(handler)},
        "retry_options": {"attempts": 1},
    }
    if vertex:
        router._client = genai.Client(
            vertexai=True, project="commsrx-ai", location="us-central1",
            credentials=Credentials("unit-test-not-a-real-token"), http_options=options,
        )
    else:
        router._client = genai.Client(
            vertexai=False, api_key="unit-test-not-a-real-key", http_options=options,
        )
    router.config = replace(config, use_vertex_ai=vertex, project_id="commsrx-ai", location="us-central1")
    return router


def assess(router):
    return GeminiGateway(router).assess(
        task_type="cv08_pv_gate", inputs={"submitted_copy": "Synthetic software verification."},
        instruction="Extract claims and safety findings.", schema=IntakeAssessment,
        offline=IntakeAssessment(claims=[], safety_findings=[]),
    )


def test_installed_vertex_sdk_accepts_typed_safety_and_preserves_structured_schema():
    calls = []
    result = {"claims": [], "safety_findings": []}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "id": "synthetic-interaction", "object": "interaction", "status": "completed",
            "model": "gemini-3.5-flash-lite",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(result)}]}],
        })

    router = sdk_router(handler)
    try:
        # Reproduce the production failure: GenerateContent settings cannot be
        # passed to the Interactions SDK, even before an HTTP request is made.
        with pytest.raises(ValueError, match="safety_settings"):
            router._client.interactions.create(
                model="gemini-3.5-flash-lite", input="Synthetic software verification.",
                safety_settings=router.get_clinical_safety_settings(),
            )
        assert not calls

        assessment, telemetry = assess(router)
        assert assessment.model_dump() == result
        assert telemetry.used_live_sdk and telemetry.transport_mode == "interactions"
        assert len(calls) == 1
        assert str(calls[0].url) == (
            "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/commsrx-ai/"
            "locations/us-central1/interactions"
        )
        body = json.loads(calls[0].content)
        assert body["safety_settings"] == [
            {"type": "dangerous_content", "threshold": "block_only_high"},
            {"type": "harassment", "threshold": "block_low_and_above"},
            {"type": "hate_speech", "threshold": "block_low_and_above"},
            {"type": "sexually_explicit", "threshold": "block_low_and_above"},
        ]
        assert body["response_format"] == {
            "type": "text", "mime_type": "application/json", "schema": IntakeAssessment.model_json_schema(),
        }
        assert body["response_mime_type"] == "application/json"
        assert body["store"] is False
    finally:
        router._client.close()


def test_installed_developer_sdk_retains_generate_content_safety_and_schema():
    calls = []
    result = {"claims": [], "safety_findings": []}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(result)}]}}],
        })

    router = sdk_router(handler, vertex=False)
    try:
        assessment, telemetry = assess(router)
        assert assessment.model_dump() == result
        assert telemetry.used_live_sdk and telemetry.transport_mode == "generate_content"
        assert len(calls) == 1
        body = json.loads(calls[0].content)
        assert body["safetySettings"] == router.get_clinical_safety_settings()
        assert body["generationConfig"]["responseJsonSchema"] == IntakeAssessment.model_json_schema()
        assert body["generationConfig"]["responseMimeType"] == "application/json"
    finally:
        router._client.close()


@pytest.mark.parametrize("status", [403, 404, 429])
def test_provider_http_status_reaches_safe_gateway_diagnostic_without_error_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {
            "code": status, "message": "private synthetic provider body must not be disclosed", "status": "ERROR",
        }})

    router = sdk_router(handler)
    try:
        with pytest.raises(ModelAssessmentError) as caught:
            assess(router)
        assert f"provider HTTP {status}" in str(caught.value)
        assert "private synthetic provider body" not in str(caught.value)
        assert calls
        assert router.telemetry_log[-1].used_live_sdk is False
    finally:
        router._client.close()


@pytest.mark.parametrize("value", [None, "404", True, 99, 600, -1])
def test_provider_status_extraction_rejects_non_http_integers(value):
    error = RuntimeError("private diagnostic")
    error.code = value
    assert GeminiModelRouter._provider_http_status(error) is None


def test_provider_status_extraction_supports_response_status():
    error = RuntimeError("private diagnostic")
    error.response = SimpleNamespace(status_code=503)
    assert GeminiModelRouter._provider_http_status(error) == 503
