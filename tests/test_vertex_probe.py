"""Cloud-free checks for the synthetic regional Vertex diagnostic helper."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "mlr_vertex_probe", Path(__file__).resolve().parents[1] / "deploy/automation/vertex_probe.py",
)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def fake_factory(payload, *, live=True, calls=None, expected_location="us-central1"):
    def factory(config):
        assert config.project_id == "commsrx-ai" and config.location == expected_location
        assert config.use_vertex_ai and config.gemini_api_key is None and config.zero_data_retention

        def create_interaction(**kwargs):
            if calls is not None:
                calls.append(kwargs)
            return payload, SimpleNamespace(
                model="gemini-4-argon", effective_model="gemini-3.1-pro-preview", used_live_sdk=live,
            )
        return SimpleNamespace(create_interaction=create_interaction)
    return factory


def test_probe_uses_only_constant_synthetic_input_and_a_closed_result_schema():
    calls = []
    result = probe.collect_probe("commsrx-ai", "us-central1", router_factory=fake_factory({"status": "unresolved"}, calls=calls))
    assert result == {
        "project_id": "commsrx-ai", "location": "us-central1", "model": "gemini-4-argon",
        "effective_model": "gemini-3.1-pro-preview", "used_live_sdk": True, "status": "passed",
        "provider_http_status": None, "error_message": "",
    }
    assert len(calls) == 1
    assert calls[0]["task_type"] == "claim_verification"
    assert calls[0]["prompt"] == probe.PROBE_PROMPT
    assert json.loads(calls[0]["prompt"]) == {
        "claim": "This is a public synthetic software connectivity check.",
        "matched_source": None, "authorized_source_passages": [],
    }
    assert calls[0]["system_instruction"] == probe.PROBE_SYSTEM_INSTRUCTION
    assert calls[0]["response_schema"] == probe.PROBE_SCHEMA
    assert calls[0]["response_schema"] == probe.ClaimVerificationVerdict.model_json_schema()
    assert "cached_content" not in calls[0]
    assert calls[0]["fallback_payload"] is None


def test_provider_error_is_bounded_redacted_and_retains_status():
    private = (
        'Bearer opaque-bearer-token api_key="opaque-api-key" access_token=opaque-access-token '
        'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.signature '
        'AIza012345678901234567890123456789012345 ya29.synthetic-oauth-token '
    )
    result = probe.collect_probe("commsrx-ai", "us-central1", router_factory=fake_factory({
        "status": "provider_error", "provider_http_status": 400, "error": private + "x" * 10000,
        "unexpected_secret_field": "never-copy-this",
    }, live=False))
    assert result["status"] == "provider_error" and result["provider_http_status"] == 400
    text = json.dumps(result)
    for secret in ("opaque-bearer-token", "opaque-api-key", "opaque-access-token", "eyJhbGci", "AIza0123", "ya29.synthetic", "never-copy-this"):
        assert secret not in text
    assert "[REDACTED]" in text
    assert len(result["error_message"]) <= probe.MAX_ERROR_LENGTH


@pytest.mark.parametrize("payload,live,status", [
    (None, False, "client_unavailable"),
    ({"status": "unresolved"}, False, "client_unavailable"),
    ({"status": "invalid-verdict"}, True, "invalid_response"),
    ("not-copied", True, "invalid_response"),
])
def test_fallback_or_wrong_schema_cannot_pass(payload, live, status):
    result = probe.collect_probe("commsrx-ai", "us-central1", router_factory=fake_factory(payload, live=live))
    assert result["status"] == status
    assert "not-copied" not in json.dumps(result)


@pytest.mark.parametrize("status", ["400", True, 99, 600])
def test_untrusted_status_values_are_not_persisted(status):
    result = probe.collect_probe("commsrx-ai", "us-central1", router_factory=fake_factory({
        "status": "provider_error", "provider_http_status": status, "error": "Invalid request",
    }, live=False))
    assert result["provider_http_status"] is None


@pytest.mark.parametrize("changes", [
    {"project_id": "other-project"}, {"location": "global"}, {"location": "other"},
    {"bucket": "other-bucket"}, {"object_name": "other-prefix/result.json"},
    {"object_name": "deployment-probes/../result.json"},
])
def test_probe_refuses_cross_project_global_or_unscoped_destinations(changes, monkeypatch):
    monkeypatch.delenv("MLR_VERTEX_PROBE_ALLOW_GLOBAL", raising=False)
    values = {
        "project_id": "commsrx-ai", "location": "us-central1",
        "bucket": "commsrx-ai-mlr-versioned-artifacts", "object_name": "deployment-probes/run-123/result.json",
        **changes,
    }
    with pytest.raises(ValueError):
        probe.validate_target(**values)


def test_private_result_upload_has_generation_zero_precondition():
    calls = []
    blob = SimpleNamespace(upload_from_string=lambda *args, **kwargs: calls.append((args, kwargs)))
    client = SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda object_name: blob))
    probe.write_result({"status": "passed"}, "commsrx-ai-mlr-versioned-artifacts", "deployment-probes/test.json", storage_client=client)
    assert calls == [(('{"status": "passed"}',), {
        "content_type": "application/json", "if_generation_match": 0,
    })]


@pytest.mark.parametrize("location", ["us", "eu", "us-central1"])
def test_probe_accepts_explicit_geography_without_changing_configured_location(location):
    probe.validate_target("commsrx-ai", location, "commsrx-ai-mlr-versioned-artifacts", "deployment-probes/test.json")
    result = probe.collect_probe("commsrx-ai", location, router_factory=fake_factory(
        {"status": "unresolved"}, expected_location=location,
    ))
    assert result["status"] == "passed"
    assert result["location"] == location


@pytest.mark.parametrize("verdict", ["supported", "partially_supported", "contradicted", "unresolved"])
def test_transport_probe_accepts_any_schema_valid_verdict_without_persisting_model_output(verdict):
    result = probe.collect_probe("commsrx-ai", "us", router_factory=fake_factory(
        {"status": verdict, "rationale": "public synthetic result must not be copied"}, expected_location="us",
    ))
    assert result["status"] == "passed" and result["used_live_sdk"] is True
    assert "public synthetic result must not be copied" not in json.dumps(result)


def set_target(monkeypatch, **changes):
    values = {
        "GOOGLE_CLOUD_PROJECT": "commsrx-ai", "GOOGLE_CLOUD_LOCATION": "us-central1",
        "MLR_VERTEX_PROBE_BUCKET": "commsrx-ai-mlr-versioned-artifacts",
        "MLR_VERTEX_PROBE_OBJECT": "deployment-probes/run-123/result.json", **changes,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_main_uploads_diagnostic_without_printing_provider_error(monkeypatch, capsys):
    set_target(monkeypatch)
    monkeypatch.setattr(probe, "collect_probe", lambda *args: {"status": "provider_error", "error_message": "private synthetic detail"})
    uploads = []
    monkeypatch.setattr(probe, "write_result", lambda *args: uploads.append(args))
    assert probe.main() == 0
    assert len(uploads) == 1
    assert "private synthetic detail" not in capsys.readouterr().out
    assert probe.os.environ["GOOGLE_GENAI_LIVE_VERTEX"] == "true"


def test_main_rejects_global_before_probe_or_upload(monkeypatch, capsys):
    set_target(monkeypatch, GOOGLE_CLOUD_LOCATION="global")
    monkeypatch.delenv("MLR_VERTEX_PROBE_ALLOW_GLOBAL", raising=False)
    monkeypatch.setattr(probe, "collect_probe", lambda *args: pytest.fail("Must not call a global model"))
    assert probe.main() == 1
    assert "configuration is invalid" in capsys.readouterr().err


def test_global_probe_is_allowed_only_by_explicit_environment_approval(monkeypatch):
    monkeypatch.setenv("MLR_VERTEX_PROBE_ALLOW_GLOBAL", "true")
    probe.validate_target("commsrx-ai", "global", "commsrx-ai-mlr-versioned-artifacts", "deployment-probes/test.json")
    result = probe.collect_probe("commsrx-ai", "global", router_factory=fake_factory(
        {"status": "unresolved"}, expected_location="global",
    ))
    assert result["status"] == "passed" and result["location"] == "global"


@pytest.mark.parametrize("value", ["false", "1", "yes", "TRUE", ""])
def test_global_probe_rejects_truthy_or_implicit_environment_values(monkeypatch, value):
    monkeypatch.setenv("MLR_VERTEX_PROBE_ALLOW_GLOBAL", value)
    with pytest.raises(ValueError, match="requires explicit"):
        probe.collect_probe("commsrx-ai", "global", router_factory=lambda *args: pytest.fail("No global call allowed"))
